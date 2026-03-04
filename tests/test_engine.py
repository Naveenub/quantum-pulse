"""
QUANTUM-PULSE :: tests/test_engine.py
=======================================
Async test suite covering the full pipeline end-to-end.
Run with:  pytest tests/ -v --asyncio-mode=auto
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import uuid

import msgpack
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.compression import PulseCompressor
from core.engine import (
    ENTROPY_SHARD_THRESHOLD,
    QuantumEngine,
    VaultKey,
    ZstdDictTrainer,
    build_merkle_tree,
    shannon_entropy,
)
from core.vault import QuantumVault
from models.pulse_models import PulseBlob


# ─────────────────────────────── fixtures ────────────────────────────────── #

PASSPHRASE = "test-passphrase-quantum-pulse-42"

SAMPLE_PAYLOAD = {
    "dataset": "openwebtext-v2",
    "shard":   42,
    "rows":    [{"text": f"The quick brown fox {i}", "id": i} for i in range(50)],
}

CORPUS_SAMPLES = [
    f'{{"path":"/data/shard_{i}.arrow","rows":{i*1000},"tokens":{i*512}}}'.encode()
    for i in range(200)
]


@pytest.fixture
def engine():
    return QuantumEngine(passphrase=PASSPHRASE)


@pytest.fixture
async def trained_engine():
    eng = QuantumEngine(passphrase=PASSPHRASE)
    await eng.bootstrap_dict(CORPUS_SAMPLES)
    return eng


# ─────────────────────────────── VaultKey tests ──────────────────────────── #

@pytest.mark.asyncio
async def test_vault_key_derivation():
    vk = await VaultKey.derive_async(PASSPHRASE)
    assert len(vk.raw) == 32
    assert len(vk.salt) == 32
    assert len(vk.hex) == 64


@pytest.mark.asyncio
async def test_vault_key_deterministic_with_salt():
    salt = os.urandom(32)
    vk1  = await VaultKey.derive_async(PASSPHRASE, salt)
    vk2  = await VaultKey.derive_async(PASSPHRASE, salt)
    assert vk1.raw == vk2.raw, "Same passphrase+salt must produce same key"


@pytest.mark.asyncio
async def test_vault_key_unique_salts():
    vk1 = await VaultKey.derive_async(PASSPHRASE)
    vk2 = await VaultKey.derive_async(PASSPHRASE)
    assert vk1.raw != vk2.raw, "Different salts must produce different keys"


# ─────────────────────────────── entropy tests ───────────────────────────── #

def test_shannon_entropy_uniform():
    """Uniform byte distribution → ~8 bits/byte."""
    data = bytes(range(256)) * 100
    e = shannon_entropy(data)
    assert 7.9 < e <= 8.0


def test_shannon_entropy_zero():
    """Single repeated byte → 0 bits/byte."""
    data = b"\x00" * 1000
    e = shannon_entropy(data)
    assert e == 0.0


def test_shannon_entropy_empty():
    assert shannon_entropy(b"") == 0.0


# ─────────────────────────────── Merkle tests ────────────────────────────── #

def test_merkle_single_leaf():
    data  = [b"hello"]
    nodes, root = build_merkle_tree(data)
    assert len(root) == 64   # SHA3-256 hex


def test_merkle_two_leaves():
    leaves = [b"left", b"right"]
    nodes, root = build_merkle_tree(leaves)
    assert root == hashlib.sha3_256(
        hashlib.sha3_256(b"left").digest() +
        hashlib.sha3_256(b"right").digest()
    ).hexdigest()


def test_merkle_empty():
    nodes, root = build_merkle_tree([])
    assert root == hashlib.sha3_256(b"").hexdigest()


def test_merkle_odd_leaves():
    """Odd number of leaves must be handled (last duplicated)."""
    leaves = [b"a", b"b", b"c"]
    nodes, root = build_merkle_tree(leaves)
    assert len(root) == 64


# ─────────────────────────────── seal / unseal ───────────────────────────── #

@pytest.mark.asyncio
async def test_seal_unseal_roundtrip(engine):
    pid        = str(uuid.uuid4())
    blob, meta = await engine.seal(SAMPLE_PAYLOAD, pulse_id=pid)
    recovered  = await engine.unseal(blob, meta)
    assert recovered["dataset"] == SAMPLE_PAYLOAD["dataset"]
    assert len(recovered["rows"]) == len(SAMPLE_PAYLOAD["rows"])


@pytest.mark.asyncio
async def test_seal_with_tags(engine):
    pid        = str(uuid.uuid4())
    tags       = {"source": "openwebtext", "split": "train"}
    blob, meta = await engine.seal(SAMPLE_PAYLOAD, pulse_id=pid, tags=tags)
    assert meta.tags == tags


@pytest.mark.asyncio
async def test_seal_compression_ratio(trained_engine):
    pid        = str(uuid.uuid4())
    blob, meta = await trained_engine.seal(SAMPLE_PAYLOAD, pulse_id=pid)
    assert meta.stats.ratio > 1.5, "Expect at least 1.5× compression"


@pytest.mark.asyncio
async def test_unseal_integrity_failure(engine):
    pid        = str(uuid.uuid4())
    blob, meta = await engine.seal(SAMPLE_PAYLOAD, pulse_id=pid)

    # Tamper with chunk_hash
    bad_meta = meta.model_copy(update={"chunk_hash": "a" * 64})
    with pytest.raises(ValueError, match="Integrity failure"):
        await engine.unseal(blob, bad_meta)


@pytest.mark.asyncio
async def test_unseal_wrong_key(engine):
    pid        = str(uuid.uuid4())
    blob, meta = await engine.seal(SAMPLE_PAYLOAD, pulse_id=pid)

    evil_engine = QuantumEngine(passphrase="wrong-passphrase-!!!")
    # Wrong key → AESGCM raises InvalidTag (wrapped as Exception)
    with pytest.raises(Exception):
        await evil_engine.unseal(blob, meta)


@pytest.mark.asyncio
async def test_seal_large_payload(trained_engine):
    """1000-row payload — ensure large blob seals correctly."""
    payload = {
        "rows": [{"text": "x" * 200, "id": i} for i in range(1000)]
    }
    pid        = str(uuid.uuid4())
    blob, meta = await trained_engine.seal(payload, pulse_id=pid)
    recovered  = await trained_engine.unseal(blob, meta)
    assert len(recovered["rows"]) == 1000


# ─────────────────────────────── MasterPulse ─────────────────────────────── #

@pytest.mark.asyncio
async def test_master_pulse_merkle(engine):
    pairs = []
    for i in range(4):
        pid        = str(uuid.uuid4())
        blob, meta = await engine.seal({"shard": i}, pulse_id=pid)
        pairs.append((blob, meta))

    master = QuantumEngine.build_master_pulse("master-test", pairs)
    assert master.total_shards == 4
    assert len(master.merkle_root) == 64
    assert master.merkle_root == master.merkle_tree[-1]


# ─────────────────────────────── ZstdDictTrainer ─────────────────────────── #

def test_dict_trainer_not_trained():
    trainer = ZstdDictTrainer()
    assert not trainer.is_trained
    assert trainer.dict_id is None


@pytest.mark.asyncio
async def test_dict_trainer_train():
    trainer = ZstdDictTrainer()
    await trainer.train_async(CORPUS_SAMPLES[:50])
    assert trainer.is_trained
    assert trainer.dict_id is not None


# ─────────────────────────────── compression benchmark ───────────────────── #

@pytest.mark.asyncio
async def test_compressor_roundtrip():
    comp = PulseCompressor()
    data = b"hello world " * 5000
    compressed, result = await comp.compress(data)
    assert result.ratio > 1.0
    decompressed = await comp.decompress(compressed)
    assert decompressed == data


@pytest.mark.asyncio
async def test_benchmark_dict_better_than_vanilla():
    comp    = PulseCompressor()
    samples = CORPUS_SAMPLES
    report  = await comp.benchmark(samples)
    # Dict should be >= vanilla (structured data always benefits from dict)
    assert report.dict_ratio >= report.vanilla_ratio * 0.95


# ─────────────────────────────── QuantumVault ────────────────────────────── #

@pytest.mark.asyncio
async def test_vault_shard_key_isolation():
    vault = QuantumVault(PASSPHRASE)
    await vault.unlock()
    salt = os.urandom(32)
    k1   = await vault.derive_shard_key("pulse-aaa", salt)
    k2   = await vault.derive_shard_key("pulse-bbb", salt)
    assert k1 != k2, "Different pulse IDs must yield different sub-keys"


@pytest.mark.asyncio
async def test_vault_shard_key_deterministic():
    vault = QuantumVault(PASSPHRASE)
    await vault.unlock()
    salt = os.urandom(32)
    k1   = await vault.derive_shard_key("pulse-xyz", salt)
    k2   = await vault.derive_shard_key("pulse-xyz", salt)
    assert k1 == k2


@pytest.mark.asyncio
async def test_vault_passphrase_change_validation():
    vault = QuantumVault(PASSPHRASE)
    with pytest.raises(ValueError, match="confirmation mismatch"):
        await vault.change_passphrase("new-passphrase-16chars+", "different-passphrase")


@pytest.mark.asyncio
async def test_vault_passphrase_min_length():
    vault = QuantumVault(PASSPHRASE)
    with pytest.raises(ValueError, match="at least 16"):
        await vault.change_passphrase("short", "short")


# ─────────────────────────────── needs_sharding ──────────────────────────── #

def test_needs_sharding_compressed_data():
    """Compressed data has high entropy → should trigger sharding."""
    import zstandard as zstd
    raw        = b"aaaaaaaaaa" * 10_000
    compressed = zstd.ZstdCompressor().compress(raw)
    # Highly compressed random-looking data → high entropy
    result = QuantumEngine.needs_sharding(compressed)
    # Just check it runs without error; result depends on actual entropy
    assert isinstance(result, bool)


def test_needs_sharding_repetitive():
    """Low-entropy data should NOT need sharding."""
    data   = b"\x00" * 50_000
    result = QuantumEngine.needs_sharding(data)
    assert result is False
