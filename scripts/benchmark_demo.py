#!/usr/bin/env python3
"""
QUANTUM-PULSE :: scripts/benchmark_demo.py
============================================
Standalone benchmark that exercises the full pipeline and prints a report.
Run from the project root:  python scripts/benchmark_demo.py
"""

import asyncio
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.compression import PulseCompressor
from core.engine import QuantumEngine, shannon_entropy
from core.vault import QuantumVault


PASSPHRASE   = "demo-passphrase-quantum-pulse-42"
NUM_SHARDS   = 10
ROWS_PER_SHARD = 200

BANNER = """
╔══════════════════════════════════════════════════════════════╗
║          QUANTUM-PULSE  ·  Full Pipeline Benchmark           ║
║    Zstd-L22 + AES-256-GCM + MsgPack + PBKDF2 + Merkle       ║
╚══════════════════════════════════════════════════════════════╝
"""


def make_payload(shard_id: int, rows: int) -> dict:
    return {
        "dataset":  "openwebtext-v2",
        "shard_id": shard_id,
        "rows": [
            {
                "id":    i,
                "text":  f"Training example {i} from shard {shard_id}: " + "x" * 80,
                "label": i % 10,
                "meta":  {"source": "web", "quality": 0.95},
            }
            for i in range(rows)
        ],
    }


async def main():
    print(BANNER)

    engine    = QuantumEngine(passphrase=PASSPHRASE)
    vault     = QuantumVault(passphrase=PASSPHRASE)
    compressor = PulseCompressor(engine._trainer)

    # ── Step 1: bootstrap Zstd dict ──────────────────────────────────────── #
    print("Step 1 · Training Zstd dictionary from corpus samples …")
    samples   = [json.dumps(make_payload(i, 5)).encode() for i in range(200)]
    t0        = time.perf_counter()
    await engine.bootstrap_dict(samples)
    print(f"         Done in {(time.perf_counter()-t0)*1000:.0f} ms\n")

    # ── Step 2: seal N shards ─────────────────────────────────────────────── #
    print(f"Step 2 · Sealing {NUM_SHARDS} shards × {ROWS_PER_SHARD} rows …")
    sealed_pairs = []
    total_original = 0
    total_encrypted = 0
    seal_times = []

    for i in range(NUM_SHARDS):
        payload  = make_payload(i, ROWS_PER_SHARD)
        pulse_id = str(uuid.uuid4())
        t0       = time.perf_counter()
        blob, meta = await engine.seal(payload, pulse_id=pulse_id, tags={"shard": str(i)})
        elapsed  = (time.perf_counter() - t0) * 1000
        seal_times.append(elapsed)
        sealed_pairs.append((blob, meta))
        total_original  += meta.stats.original_bytes
        total_encrypted += meta.stats.encrypted_bytes
        print(
            f"         Shard {i:02d}  "
            f"{meta.stats.original_bytes/1024:6.1f} KiB → "
            f"{meta.stats.encrypted_bytes/1024:6.1f} KiB  "
            f"ratio={meta.stats.ratio:.2f}×  "
            f"{elapsed:.0f} ms"
        )

    avg_ratio = total_original / max(total_encrypted, 1)
    avg_ms    = sum(seal_times) / len(seal_times)
    print(f"\n         Average ratio: {avg_ratio:.2f}×  |  Avg time: {avg_ms:.0f} ms/shard\n")

    # ── Step 3: build MasterPulse ────────────────────────────────────────── #
    print("Step 3 · Building MasterPulse Merkle tree …")
    master = QuantumEngine.build_master_pulse("master-benchmark", sealed_pairs)
    print(f"         Shards:      {master.total_shards}")
    print(f"         Tree nodes:  {len(master.merkle_tree)}")
    print(f"         Merkle root: {master.merkle_root[:32]}…\n")

    # ── Step 4: unseal all shards and verify ─────────────────────────────── #
    print("Step 4 · Unsealing + verifying all shards …")
    unseal_times = []
    for i, (blob, meta) in enumerate(sealed_pairs):
        t0       = time.perf_counter()
        payload  = await engine.unseal(blob, meta)
        elapsed  = (time.perf_counter() - t0) * 1000
        unseal_times.append(elapsed)
        assert len(payload["rows"]) == ROWS_PER_SHARD, f"Row count mismatch shard {i}"
    avg_unseal = sum(unseal_times) / len(unseal_times)
    print(f"         All {NUM_SHARDS} shards verified ✓  avg unseal: {avg_unseal:.0f} ms\n")

    # ── Step 5: compression benchmark (vanilla vs dict) ───────────────────── #
    print("Step 5 · Zstd benchmark: vanilla vs dictionary …")
    bench_samples = [json.dumps(make_payload(i, 20)).encode() for i in range(50)]
    report = await compressor.benchmark(bench_samples)
    print(f"         Vanilla ratio:  {report.vanilla_ratio:.3f}×  ({report.vanilla_ms:.0f} ms)")
    print(f"         Dict ratio:     {report.dict_ratio:.3f}×  ({report.dict_ms:.0f} ms)")
    improvement = report.improvement_pct
    sign = "+" if improvement >= 0 else ""
    print(f"         Improvement:    {sign}{improvement:.1f}%\n")

    # ── Step 6: vault sub-key derivation ─────────────────────────────────── #
    print("Step 6 · Vault HKDF sub-key derivation …")
    await vault.unlock()
    import os as _os
    salt = _os.urandom(32)
    t0   = time.perf_counter()
    for pid in [str(uuid.uuid4()) for _ in range(10)]:
        await vault.derive_shard_key(pid, salt)
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"         10 sub-keys derived in {elapsed:.0f} ms")
    print(f"         Cache size: {len(vault._cache)} entries\n")

    # ── Summary ──────────────────────────────────────────────────────────── #
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║                    BENCHMARK SUMMARY                        ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    print(f"║  Total shards sealed:    {NUM_SHARDS:>4}                                ║")
    print(f"║  Rows per shard:         {ROWS_PER_SHARD:>4}                                ║")
    print(f"║  Original data:        {total_original/1024:>6.1f} KiB                           ║")
    print(f"║  Encrypted data:       {total_encrypted/1024:>6.1f} KiB                           ║")
    print(f"║  Compression ratio:      {avg_ratio:>4.2f}×                               ║")
    print(f"║  Avg seal latency:      {avg_ms:>5.0f} ms                              ║")
    print(f"║  Avg unseal latency:    {avg_unseal:>5.0f} ms                              ║")
    print(f"║  Dict improvement:      {sign}{improvement:>4.1f}%                              ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print("\n✓ All systems nominal — QUANTUM-PULSE is ready.\n")


if __name__ == "__main__":
    asyncio.run(main())
