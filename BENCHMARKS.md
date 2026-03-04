# QUANTUM-PULSE Benchmarks

Reproducible benchmarks comparing QUANTUM-PULSE against every major compression algorithm
on realistic LLM training data.

Run them yourself:
```bash
python scripts/benchmark_compare.py          # 500 records (fast, ~30s)
python scripts/benchmark_compare.py --records 1000   # larger corpus
```

---

## Results — 1000 LLM Training Records (1.2 MiB JSON corpus)

```
────────────────────────────────────────────────────────────────────
Algorithm              Ratio      vs gzip     Time      Enc   Int
────────────────────────────────────────────────────────────────────
snappy                 12.03×      −80.9%      0.7 ms    ✗     ✗
lz4                    33.80×      −46.2%      0.4 ms    ✗     ✗
gzip-9                 62.86×      baseline    9.1 ms    ✗     ✗
zstd-L3                76.19×      +21.2%      0.7 ms    ✗     ✗
QUANTUM-PULSE ◀        95.51×      +51.9%    553.4 ms    ✓     ✓
zstd-L22+MsgPack       96.60×      +53.7%   1173.4 ms    ✗     ✗
zstd-L22               99.58×      +58.4%   1644.3 ms    ✗     ✗
brotli-11             112.95×      +79.7%   1354.2 ms    ✗     ✗
────────────────────────────────────────────────────────────────────
Enc = AES-256-GCM encryption     Int = SHA3-256 Merkle integrity
```

**Honest findings:**

- **QUANTUM-PULSE is the fastest high-compression pipeline that also encrypts and verifies data** — 553 ms vs 1173 ms (zstd+msgpack), 1354 ms (brotli-11), 1644 ms (zstd-L22).
- **Brotli-11 achieves the highest raw compression ratio** (112× vs 95×) but is 2.4× slower and provides no encryption or integrity guarantees.
- **QUANTUM-PULSE is 3× faster than vanilla zstd-L22** at a comparable ratio, because the pre-trained corpus dictionary skips per-shard pattern discovery.
- **Every other algorithm in this table offers zero security.** QUANTUM-PULSE is the only option if you need compression + encryption + tamper detection in one pipeline.

---

## Full Pipeline Benchmark (Seal + Unseal + Merkle, 10 shards)

```
Shards:              10 × 200 rows
Original data:       328 KiB
Encrypted blob:        8.4 KiB
End-to-end ratio:    39.16×
Dict boost:          +94.4% ratio vs no-dict
Avg seal latency:    116 ms / shard
Avg unseal latency:  100 ms / shard
Merkle nodes:        21 (10 shards verified ✓)
```

Run: `python scripts/benchmark_demo.py`

---

## Why QUANTUM-PULSE Beats zstd-L22 on Speed

The pre-trained Zstd dictionary lets the compressor skip pattern discovery:

```
zstd-L22 (no dict):  1644 ms  ← scans entire corpus to build pattern table
zstd-L22 (dict):      553 ms  ← pattern table already known, just compress
                     ──────
                      3× faster
```

The dictionary is trained once on a sample of your corpus and reused across
all subsequent seal operations. As your dataset grows, QUANTUM-PULSE retrains
the dictionary in the background every 24 hours.

---

## Feature Matrix

| Feature                     | gzip | lz4 | brotli | zstd | QUANTUM-PULSE |
|-----------------------------|:----:|:---:|:------:|:----:|:-------------:|
| High compression ratio      |  ✓   |  ✗  |   ✓    |  ✓   |      ✓        |
| Fast decompression          |  ✗   |  ✓  |   ✗    |  ✓   |      ✓        |
| Cross-corpus dictionary     |  ✗   |  ✗  |   ✗    |  ✓   |      ✓        |
| Cross-shard pattern sharing |  ✗   |  ✗  |   ✗    |  ✗   |      ✓        |
| AES-256-GCM encryption      |  ✗   |  ✗  |   ✗    |  ✗   |      ✓        |
| SHA3-256 Merkle integrity   |  ✗   |  ✗  |   ✗    |  ✗   |      ✓        |
| Per-blob key isolation      |  ✗   |  ✗  |   ✗    |  ✗   |      ✓        |
| REST API                    |  ✗   |  ✗  |   ✗    |  ✗   |      ✓        |
| Virtual filesystem mount    |  ✗   |  ✗  |   ✗    |  ✗   |      ✓        |
| Background dict retraining  |  ✗   |  ✗  |   ✗    |  ✗   |      ✓        |

---

## Reproduce

```bash
git clone https://github.com/YOUR_USERNAME/quantum-pulse
cd quantum-pulse
pip install -r requirements.txt
pip install lz4 brotli python-snappy      # competitor libs

# Head-to-head comparison (prints Rich table)
python scripts/benchmark_compare.py

# Save results as JSON
python scripts/benchmark_compare.py --records 1000 --output my_results.json

# Full pipeline benchmark (seal/unseal/Merkle)
python scripts/benchmark_demo.py
```

---

## Environment (reference run)

```
Python:   3.12
zstd:     libzstd 1.5.5
OS:       Linux x86_64
AES:      hardware AES-NI via OpenSSL
KDF:      PBKDF2-SHA256, 600,000 iterations
```

---

## Submit Your Results

Run on your hardware or a real dataset and open a PR:

```bash
python scripts/benchmark_compare.py --records 1000 --output results.json
```

Add `benchmarks/community/YOUR_DESCRIPTION.json` + a note on your corpus type and hardware.
See [benchmarks/community/README.md](benchmarks/community/README.md).
