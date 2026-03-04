"""
QUANTUM-PULSE :: core/engine.py
================================
Central compression-crypto-serialisation engine.

Pipeline  →  Raw Object → MsgPack → Zstd(L22+dict) → AES-256-GCM → Wire Blob
Reverse   →  Wire Blob  → AES-256-GCM → Zstd Decomp → MsgPack → Python Object

Key design decisions
────────────────────
• MsgPack       binary, ~30 % smaller than JSON, zero-copy friendly
• Zstd L22      "Ultra" preset + pre-trained per-corpus dictionary
• AES-256-GCM   AEAD via OpenSSL backend → AES-NI hardware path
• PBKDF2-SHA256 600 000 iters (OWASP 2024), fresh salt per blob
• SHA3-256       Merkle tree over ciphertext leaves
• Async-first   all CPU work dispatched to ThreadPoolExecutor
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import math
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any, AsyncIterator, Final, Optional, Sequence

import msgpack
import zstandard as zstd
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from loguru import logger
from pydantic import BaseModel

from models.pulse_models import (
    CompressionStats,
    MasterPulse,
    PulseBlob,
    PulseStatus,
)

# Adaptive dict manager — imported lazily to avoid circular dependency
_ADAPTIVE_CLS = None
def _adaptive_cls():
    global _ADAPTIVE_CLS
    if _ADAPTIVE_CLS is None:
        from core.adaptive import AdaptiveDictManager  # noqa: PLC0415
        _ADAPTIVE_CLS = AdaptiveDictManager
    return _ADAPTIVE_CLS

# ─────────────────────────────── constants ────────────────────────────────── #

ZSTD_LEVEL:              Final[int]   = 22
ZSTD_WINDOW_LOG:         Final[int]   = 27          # 128 MiB sliding window
ZSTD_THREADS:            Final[int]   = os.cpu_count() or 4
ZSTD_DICT_SAMPLE_RATIO:  Final[float] = 0.05        # first 5 % of corpus

AES_KEY_BYTES:           Final[int]   = 32           # 256-bit
AES_NONCE_BYTES:         Final[int]   = 12           # 96-bit GCM nonce
AES_TAG_BYTES:           Final[int]   = 16           # 128-bit auth tag

KDF_ITERATIONS:          Final[int]   = 600_000      # OWASP 2024
KDF_SALT_BYTES:          Final[int]   = 32

BLOB_MAGIC:              Final[bytes] = b"QPLS"
BLOB_VERSION:            Final[int]   = 1

ENTROPY_SHARD_THRESHOLD: Final[float] = 0.92        # bits/byte
GRIDFS_THRESHOLD_BYTES:  Final[int]   = 16 * 1024 * 1024

_CPU_POOL = ThreadPoolExecutor(
    max_workers=ZSTD_THREADS,
    thread_name_prefix="qp-cpu",
)

# ─────────────────────────────── wire format ──────────────────────────────── #
#
#  ┌──────────┬───────┬─────────────┬────────────────────────────────┐
#  │ MAGIC 4B │ VER1B │  NONCE 12B  │  CIPHERTEXT + GCM-TAG (var)   │
#  └──────────┴───────┴─────────────┴────────────────────────────────┘
#
HEADER_FMT:  Final[str] = f"!4sB{AES_NONCE_BYTES}s"
HEADER_SIZE: Final[int] = struct.calcsize(HEADER_FMT)   # 17 bytes


def _pack_header(nonce: bytes) -> bytes:
    return struct.pack(HEADER_FMT, BLOB_MAGIC, BLOB_VERSION, nonce)


def _unpack_header(raw: bytes) -> tuple[int, bytes]:
    magic, version, nonce = struct.unpack_from(HEADER_FMT, raw)
    if magic != BLOB_MAGIC:
        raise ValueError(f"Bad magic {magic!r}; expected {BLOB_MAGIC!r}")
    return version, nonce


# ─────────────────────────────── helpers ──────────────────────────────────── #

def shannon_entropy(data: bytes) -> float:
    """Shannon entropy in bits/byte — pure Python, no deps."""
    if not data:
        return 0.0
    freq = [0] * 256
    for b in data:
        freq[b] += 1
    n = len(data)
    e = 0.0
    for c in freq:
        if c:
            p = c / n
            e -= p * math.log2(p)
    return e


def build_merkle_tree(leaves: Sequence[bytes]) -> tuple[list[str], str]:
    """
    Build a binary Merkle tree from SHA3-256 leaf hashes.
    Returns (all_node_hashes_hex, root_hex).
    """
    if not leaves:
        empty = hashlib.sha3_256(b"").hexdigest()
        return [empty], empty

    layer: list[bytes] = [hashlib.sha3_256(leaf).digest() for leaf in leaves]
    all_nodes = [n.hex() for n in layer]

    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])          # duplicate last leaf if odd
        next_layer: list[bytes] = []
        for i in range(0, len(layer), 2):
            next_layer.append(hashlib.sha3_256(layer[i] + layer[i + 1]).digest())
        all_nodes.extend(n.hex() for n in next_layer)
        layer = next_layer

    return all_nodes, layer[0].hex()


@lru_cache(maxsize=32)
def _cached_aesgcm(key_hex: str) -> AESGCM:
    """Cache AESGCM objects to amortise construction; routes to AES-NI via OpenSSL."""
    return AESGCM(bytes.fromhex(key_hex))


def _sizeof_fmt(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PiB"


# ─────────────────────────────── VaultKey ─────────────────────────────────── #

class VaultKey:
    """
    PBKDF2-SHA256 derived 256-bit key.
    Salt is generated fresh on construction unless provided (for re-derivation).
    The raw key bytes live only in process memory.
    """

    __slots__ = ("_key", "_salt")

    def __init__(self, passphrase: str, salt: Optional[bytes] = None) -> None:
        self._salt: bytes = salt if salt is not None else os.urandom(KDF_SALT_BYTES)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=AES_KEY_BYTES,
            salt=self._salt,
            iterations=KDF_ITERATIONS,
        )
        self._key: bytes = kdf.derive(passphrase.encode("utf-8"))
        logger.debug("VaultKey derived  salt={}…", self._salt.hex()[:12])

    @property
    def raw(self)      -> bytes: return self._key
    @property
    def hex(self)      -> str:   return self._key.hex()
    @property
    def salt(self)     -> bytes: return self._salt
    @property
    def salt_hex(self) -> str:   return self._salt.hex()

    @classmethod
    async def derive_async(
        cls, passphrase: str, salt: Optional[bytes] = None
    ) -> "VaultKey":
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_CPU_POOL, cls, passphrase, salt)


# ─────────────────────────────── ZstdDictTrainer ──────────────────────────── #

class ZstdDictTrainer:
    """
    Trains a Zstd compression dictionary from a corpus sample set.
    The trained dict is reused for all subsequent compress/decompress calls,
    achieving 2-4× better ratios than vanilla Zstd on structured LLM data.
    """

    def __init__(self, dict_size: int = 112 * 1024) -> None:
        self._dict_size = dict_size
        self._cdict: Optional[zstd.ZstdCompressionDict] = None

    def train(self, samples: list[bytes]) -> None:
        logger.info("Training Zstd dict on {} samples …", len(samples))
        self._cdict = zstd.train_dictionary(self._dict_size, samples)
        logger.success(
            "Zstd dict ready  id={}  size={}",
            self._cdict.dict_id(),
            _sizeof_fmt(len(self._cdict.as_bytes())),
        )

    async def train_async(self, samples: list[bytes]) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_CPU_POOL, self.train, samples)

    @property
    def dict_id(self) -> Optional[int]:
        return self._cdict.dict_id() if self._cdict else None

    @property
    def is_trained(self) -> bool:
        return self._cdict is not None

    def compressor(self) -> zstd.ZstdCompressor:
        params = zstd.ZstdCompressionParameters.from_level(
            ZSTD_LEVEL,
            window_log=ZSTD_WINDOW_LOG,
            threads=ZSTD_THREADS,
        )
        return zstd.ZstdCompressor(compression_params=params, dict_data=self._cdict)

    def decompressor(self) -> zstd.ZstdDecompressor:
        return zstd.ZstdDecompressor(dict_data=self._cdict)

    def compressor_params_info(self) -> dict:
        return {
            "level": ZSTD_LEVEL,
            "window_log": ZSTD_WINDOW_LOG,
            "threads": ZSTD_THREADS,
            "dict_id": self.dict_id,
        }


# ─────────────────────────────── QuantumEngine ────────────────────────────── #

class QuantumEngine:
    """
    Thread-safe, async-first core pipeline.

    Seal path:   object → msgpack → zstd → aes-gcm → bytes + PulseBlob
    Unseal path: bytes + PulseBlob → aes-gcm → zstd → msgpack → object
    """

    def __init__(
        self,
        passphrase: str,
        *,
        dict_trainer: Optional[ZstdDictTrainer] = None,
        adaptive_dict = None,   # Optional[AdaptiveDictManager]
        aad: bytes = b"QUANTUM-PULSE-v1",
    ) -> None:
        self._passphrase  = passphrase
        self._trainer     = dict_trainer or ZstdDictTrainer()
        self._adaptive    = adaptive_dict  # wired in by main.py lifespan
        self._aad         = aad
        logger.info(
            "QuantumEngine ready  zstd=L{}  window={}  aes=256-GCM  kdf=PBKDF2-{}k",
            ZSTD_LEVEL, _sizeof_fmt(2 ** ZSTD_WINDOW_LOG), KDF_ITERATIONS // 1000,
        )

    # ── public API ─────────────────────────────────────────────────────────── #

    async def seal(
        self,
        payload: Any,
        *,
        pulse_id: str,
        parent_id: Optional[str] = None,
        tags: Optional[dict[str, str]] = None,
    ) -> tuple[bytes, PulseBlob]:
        """Serialise → compress → encrypt.  Returns (wire_blob, PulseBlob)."""
        t0   = time.perf_counter()
        loop = asyncio.get_running_loop()

        # 1. MsgPack
        packed = msgpack.packb(payload, use_bin_type=True)

        # 1b. Feed sample to adaptive dict manager (non-blocking, best-effort)
        if self._adaptive is not None:
            asyncio.ensure_future(self._adaptive.record_seal(packed))

        # 2. Zstd (CPU-heavy → pool)
        compressed: bytes = await loop.run_in_executor(_CPU_POOL, self._compress, packed)

        # 3. Entropy measurement
        entropy = shannon_entropy(compressed)

        # 4. Fresh key per blob (forward secrecy)
        vk = await VaultKey.derive_async(self._passphrase)

        # 5. AES-256-GCM (CPU → pool)
        nonce      = os.urandom(AES_NONCE_BYTES)
        ciphertext = await loop.run_in_executor(
            _CPU_POOL, self._encrypt, compressed, vk.raw, nonce
        )

        # 6. Assemble wire blob
        blob           = _pack_header(nonce) + ciphertext
        chunk_hash     = hashlib.sha3_256(ciphertext).hexdigest()
        _, merkle_root = build_merkle_tree([ciphertext])

        duration_ms = (time.perf_counter() - t0) * 1_000
        stats = CompressionStats(
            original_bytes   = len(packed),
            packed_bytes     = len(packed),
            compressed_bytes = len(compressed),
            encrypted_bytes  = len(blob),
            duration_ms      = duration_ms,
            entropy_bits_per_byte = entropy,
        )
        dict_ver = self._adaptive.current_version if self._adaptive else 0
        meta = PulseBlob(
            pulse_id     = pulse_id,
            parent_id    = parent_id,
            merkle_root  = merkle_root,
            chunk_hash   = chunk_hash,
            salt         = vk.salt_hex,
            nonce        = nonce.hex(),
            zstd_dict_id = (self._adaptive.dict_id if self._adaptive and self._adaptive.is_trained
                            else self._trainer.dict_id),
            dict_version = dict_ver,
            stats        = stats,
            tags         = tags or {},
        )
        logger.success(
            "Sealed  id={}  {}→{}  ratio={:.2f}×  entropy={:.3f}  {:.1f} ms",
            pulse_id[:8],
            _sizeof_fmt(len(packed)),
            _sizeof_fmt(len(blob)),
            stats.ratio,
            entropy,
            duration_ms,
        )
        return blob, meta

    async def unseal(self, blob: bytes, meta: PulseBlob) -> Any:
        """Verify → decrypt → decompress → deserialise."""
        t0   = time.perf_counter()
        loop = asyncio.get_running_loop()

        _ver, nonce = _unpack_header(blob)
        ciphertext  = blob[HEADER_SIZE:]

        # Pre-decryption integrity check
        actual_hash = hashlib.sha3_256(ciphertext).hexdigest()
        if not hmac.compare_digest(actual_hash, meta.chunk_hash):
            raise ValueError(
                f"Integrity failure for pulse {meta.pulse_id}: "
                f"expected {meta.chunk_hash!r}, got {actual_hash!r}"
            )

        # Re-derive key using stored salt
        vk = await VaultKey.derive_async(
            self._passphrase, bytes.fromhex(meta.salt)
        )

        # Decrypt (AESGCM raises InvalidTag on auth failure)
        compressed: bytes = await loop.run_in_executor(
            _CPU_POOL, self._decrypt, ciphertext, vk.raw, nonce
        )

        # Decompress (use dict version recorded at seal time)
        dict_ver = getattr(meta, "dict_version", 0)
        packed: bytes = await loop.run_in_executor(
            _CPU_POOL, self._decompress, compressed, dict_ver
        )

        # MsgPack deserialise
        payload = msgpack.unpackb(packed, raw=False)

        logger.info(
            "Unsealed  id={}  {}  {:.1f} ms",
            meta.pulse_id[:8], _sizeof_fmt(len(packed)),
            (time.perf_counter() - t0) * 1_000,
        )
        return payload

    async def unseal_stream(
        self,
        blob_stream: AsyncIterator[bytes],
        meta: PulseBlob,
    ) -> AsyncIterator[bytes]:
        """
        Collect a chunked blob stream, decrypt, and yield MsgPack bytes.
        Used by the FastAPI streaming endpoint for FUSE-like virtual reads.
        GCM requires the full ciphertext before authentication can pass.
        """
        buf = io.BytesIO()
        async for chunk in blob_stream:
            buf.write(chunk)
        payload = await self.unseal(buf.getvalue(), meta)
        yield msgpack.packb(payload, use_bin_type=True)

    async def bootstrap_dict(self, raw_samples: list[bytes]) -> None:
        """Train Zstd dict from corpus samples; also feeds the adaptive manager."""
        n      = max(1, int(len(raw_samples) * ZSTD_DICT_SAMPLE_RATIO))
        subset = raw_samples[:n]
        # Feed adaptive manager (preferred path)
        if self._adaptive is not None:
            for s in subset:
                self._adaptive._buffer.append(s)
            result = await self._adaptive.force_retrain(extra_samples=None)
            if result and result.committed:
                logger.info("Adaptive dict bootstrapped  v{}  ratio={:.2f}×",
                            result.new_version, result.new_ratio)
                return
        # Fallback to legacy trainer
        await self._trainer.train_async(subset)
        logger.info("Dict bootstrap done  samples_used={}", n)

    @staticmethod
    def build_master_pulse(
        master_id: str,
        sub_blobs: list[tuple[bytes, PulseBlob]],
    ) -> MasterPulse:
        """Build a Merkle-indexed MasterPulse from a list of (blob, meta) pairs."""
        ciphertexts  = [b[HEADER_SIZE:] for b, _ in sub_blobs]
        tree, root   = build_merkle_tree(ciphertexts)
        total_bytes  = sum(m.stats.original_bytes for _, m in sub_blobs)
        return MasterPulse(
            master_id            = master_id,
            shard_ids            = [m.pulse_id for _, m in sub_blobs],
            merkle_tree          = tree,
            merkle_root          = root,
            total_original_bytes = total_bytes,
            total_shards         = len(sub_blobs),
        )

    @staticmethod
    def needs_sharding(packed: bytes) -> bool:
        """Return True when payload entropy exceeds shard threshold."""
        h = shannon_entropy(packed)
        logger.debug("Entropy: {:.4f} b/B  threshold={}", h, ENTROPY_SHARD_THRESHOLD)
        return h >= ENTROPY_SHARD_THRESHOLD

    # ── sync internals (run in thread pool) ────────────────────────────────── #

    def _compress(self, data: bytes) -> bytes:
        if self._adaptive is not None and self._adaptive.is_trained:
            return self._adaptive.compressor().compress(data)
        return self._trainer.compressor().compress(data)

    def _decompress(self, data: bytes, dict_version: int = 0) -> bytes:
        if self._adaptive is not None and dict_version > 0:
            return self._adaptive.decompressor_for_version(dict_version).decompress(data)
        if self._adaptive is not None and self._adaptive.is_trained:
            # current version decompressor
            dv = self._adaptive.current_version
            return self._adaptive.decompressor_for_version(dv).decompress(data)
        return self._trainer.decompressor().decompress(data)

    def _encrypt(self, plaintext: bytes, key: bytes, nonce: bytes) -> bytes:
        return _cached_aesgcm(key.hex()).encrypt(nonce, plaintext, self._aad)

    def _decrypt(self, ciphertext: bytes, key: bytes, nonce: bytes) -> bytes:
        return _cached_aesgcm(key.hex()).decrypt(nonce, ciphertext, self._aad)
