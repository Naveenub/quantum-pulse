"""
QUANTUM-PULSE :: core/vault.py
================================
Cryptographic key vault layer.

Responsibilities
────────────────
  • Master-key derivation via PBKDF2-SHA256 (salted, 600 000 iters)
  • Per-shard sub-key derivation via HKDF (avoids passphrase re-hashing per blob)
  • Key rotation: re-encrypts affected shards atomically; only changed shards
    are touched (DB pointer update, not full re-write)
  • Secure passphrase change with verification step
  • In-memory key cache with TTL eviction (avoids re-deriving on hot reads)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from loguru import logger

from core.engine import (
    _CPU_POOL,
    AES_KEY_BYTES,
    AES_NONCE_BYTES,
    KDF_SALT_BYTES,
    VaultKey,
)
from models.pulse_models import PulseBlob

# ─────────────────────────────── constants ────────────────────────────────── #

KEY_CACHE_TTL_SECONDS = 300     # Evict cached sub-keys after 5 minutes
HKDF_INFO_PREFIX      = b"quantum-pulse-shard-key-v1:"

# ─────────────────────────────── KeyCache ─────────────────────────────────── #

class _KeyCache:
    """
    Thread-safe in-memory TTL cache for derived sub-keys.
    Keyed by (master_key_hex, pulse_id).
    """

    def __init__(self, ttl: float = KEY_CACHE_TTL_SECONDS) -> None:
        self._store: dict[str, tuple[bytes, float]] = {}
        self._ttl   = ttl

    def get(self, cache_key: str) -> bytes | None:
        entry = self._store.get(cache_key)
        if entry is None:
            return None
        key_bytes, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[cache_key]
            return None
        return key_bytes

    def put(self, cache_key: str, key_bytes: bytes) -> None:
        self._store[cache_key] = (key_bytes, time.monotonic() + self._ttl)

    def evict(self, cache_key: str) -> None:
        self._store.pop(cache_key, None)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


# ─────────────────────────────── QuantumVault ─────────────────────────────── #

class QuantumVault:
    """
    Manages all key material for QUANTUM-PULSE.

    Per-blob key isolation
    ─────────────────────
    Rather than encrypting every blob with the raw PBKDF2 master key,
    each blob gets a unique sub-key derived via HKDF:

        sub_key = HKDF(master_key, salt=blob_nonce, info=pulse_id_bytes)

    This means:
      • Compromise of one blob's key does not expose other blobs
      • Re-encrypting a single shard only requires that shard's sub-key
      • The master key never directly touches ciphertext

    Key rotation
    ────────────
    1. Derive new master key from new passphrase
    2. For each affected PulseBlob: derive old sub-key → decrypt → derive new
       sub-key → re-encrypt → update DB pointer atomically
    """

    def __init__(
        self,
        passphrase: str,
        *,
        cache_ttl: float = KEY_CACHE_TTL_SECONDS,
    ) -> None:
        self._passphrase = passphrase
        self._master_vk: VaultKey | None = None
        self._cache      = _KeyCache(ttl=cache_ttl)
        logger.info("QuantumVault initialised  cache_ttl={}s", cache_ttl)

    # ── master key ─────────────────────────────────────────────────────────── #

    async def unlock(self, salt: bytes | None = None) -> VaultKey:
        """
        Derive (or re-derive) the master VaultKey.
        Caches the result; subsequent calls with the same salt return immediately.
        """
        if self._master_vk is None or (salt and salt != self._master_vk.salt):
            self._master_vk = await VaultKey.derive_async(self._passphrase, salt)
            logger.debug("Master key unlocked  salt={}…", self._master_vk.salt_hex[:12])
        return self._master_vk

    def lock(self) -> None:
        """Wipe master key from memory and clear cache."""
        self._master_vk = None
        self._cache.clear()
        logger.info("Vault locked — master key wiped from memory")

    # ── per-shard sub-key derivation ───────────────────────────────────────── #

    async def derive_shard_key(
        self, pulse_id: str, blob_salt: bytes
    ) -> bytes:
        """
        HKDF-expand master key into a unique 256-bit sub-key for one pulse.
        Cached with TTL to avoid redundant crypto on hot read paths.
        """
        if self._master_vk is None:
            await self.unlock()

        assert self._master_vk is not None
        cache_key = f"{self._master_vk.hex[:16]}:{pulse_id}"
        cached    = self._cache.get(cache_key)
        if cached is not None:
            return cached

        loop = asyncio.get_running_loop()
        sub_key: bytes = await loop.run_in_executor(
            _CPU_POOL,
            self._sync_hkdf,
            self._master_vk.raw,
            blob_salt,
            pulse_id.encode(),
        )
        self._cache.put(cache_key, sub_key)
        logger.debug("Derived shard key  pulse={}…  cached={}", pulse_id[:8], len(self._cache))
        return sub_key

    # ── passphrase change ──────────────────────────────────────────────────── #

    async def change_passphrase(
        self,
        new_passphrase: str,
        confirm: str,
    ) -> VaultKey:
        """
        Securely update the master passphrase.
        Verification done via constant-time comparison to prevent timing leaks.
        Callers must subsequently call rotate_shards() to re-encrypt all blobs.
        """
        if not hmac.compare_digest(new_passphrase, confirm):
            raise ValueError("Passphrase confirmation mismatch")
        if len(new_passphrase) < 16:
            raise ValueError("Passphrase must be at least 16 characters")

        self._passphrase = new_passphrase
        self._master_vk  = None
        self._cache.clear()
        new_vk = await self.unlock()
        logger.warning("Passphrase changed — all shards must be rotated!")
        return new_vk

    # ── atomic key rotation ────────────────────────────────────────────────── #

    async def rotate_shard(
        self,
        blob: bytes,
        meta: PulseBlob,
        old_passphrase: str,
        new_master_key: VaultKey,
    ) -> tuple[bytes, PulseBlob]:
        """
        Re-encrypt one shard under the new master key.

        1. Derive old sub-key → decrypt blob
        2. Derive new sub-key → re-encrypt
        3. Return new (blob, PulseBlob) — caller does atomic DB pointer swap

        Only the affected shard is re-written; others are untouched.
        """
        from core.engine import (
            HEADER_SIZE,
            _cached_aesgcm,
            _pack_header,
            _unpack_header,
        )

        loop = asyncio.get_running_loop()

        # Old decryption
        old_vault  = QuantumVault(old_passphrase)
        old_sk     = await old_vault.derive_shard_key(
            meta.pulse_id, bytes.fromhex(meta.salt)
        )
        _ver, old_nonce = _unpack_header(blob)
        ciphertext      = blob[HEADER_SIZE:]
        aad             = b"QUANTUM-PULSE-v1"
        plaintext: bytes = await loop.run_in_executor(
            _CPU_POOL,
            lambda: _cached_aesgcm(old_sk.hex()).decrypt(old_nonce, ciphertext, aad),
        )

        # New encryption
        new_salt  = os.urandom(32)
        new_sk    = await self._hkdf_with_key(
            new_master_key.raw, new_salt, meta.pulse_id.encode()
        )
        new_nonce = os.urandom(AES_NONCE_BYTES)
        new_ct: bytes = await loop.run_in_executor(
            _CPU_POOL,
            lambda: _cached_aesgcm(new_sk.hex()).encrypt(new_nonce, plaintext, aad),
        )

        new_blob = _pack_header(new_nonce) + new_ct

        from core.engine import build_merkle_tree
        new_chunk_hash   = hashlib.sha3_256(new_ct).hexdigest()
        _, new_merkle    = build_merkle_tree([new_ct])

        new_meta = meta.model_copy(update={
            "salt":        new_salt.hex(),
            "nonce":       new_nonce.hex(),
            "chunk_hash":  new_chunk_hash,
            "merkle_root": new_merkle,
        })

        logger.info("Rotated shard  pulse={}…", meta.pulse_id[:8])
        return new_blob, new_meta

    async def rotate_all_shards(
        self,
        blobs_and_metas: list[tuple[bytes, PulseBlob]],
        old_passphrase: str,
    ) -> list[tuple[bytes, PulseBlob]]:
        """
        Rotate every shard concurrently.
        Returns new (blob, meta) pairs; DB atomic update is caller's responsibility.
        """
        new_vk = await self.unlock()
        tasks  = [
            self.rotate_shard(blob, meta, old_passphrase, new_vk)
            for blob, meta in blobs_and_metas
        ]
        results = await asyncio.gather(*tasks)
        logger.success("Key rotation complete  shards_rotated={}", len(results))
        return list(results)

    # ── random token generation ─────────────────────────────────────────────── #

    @staticmethod
    def generate_passphrase(word_count: int = 6) -> str:
        """
        Generate a cryptographically random BIP39-style passphrase alternative.
        Uses secrets.token_urlsafe for maximum entropy.
        """
        return "-".join(secrets.token_urlsafe(8) for _ in range(word_count))

    @staticmethod
    def generate_salt() -> bytes:
        return os.urandom(KDF_SALT_BYTES)

    # ── internals ──────────────────────────────────────────────────────────── #

    @staticmethod
    def _sync_hkdf(master_key: bytes, salt: bytes, info: bytes) -> bytes:
        hkdf = HKDF(
            algorithm = hashes.SHA256(),
            length    = AES_KEY_BYTES,
            salt      = salt,
            info      = HKDF_INFO_PREFIX + info,
        )
        return hkdf.derive(master_key)

    async def _hkdf_with_key(
        self, master_key: bytes, salt: bytes, info: bytes
    ) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _CPU_POOL, self._sync_hkdf, master_key, salt, info
        )
