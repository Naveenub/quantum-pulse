# ⚡ QUANTUM-PULSE

> **Extreme-density encrypted data vault for LLM training pipelines.**  
> MsgPack + Zstd-L22 + corpus dictionary + AES-256-GCM + SHA3-256 Merkle trees + REST API

[![CI](https://github.com/Naveenub/quantum-pulse/actions/workflows/ci.yml/badge.svg)](https://github.com/Naveenub/quantum-pulse/actions)
[![PyPI version](https://badge.fury.io/py/quantum-pulse.svg)](https://pypi.org/project/quantum-pulse/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-277%20passing-brightgreen.svg)](#testing)
[![Coverage](https://img.shields.io/badge/coverage-81%25-brightgreen.svg)](#testing)

---

## Demo

<div align="center">

![QUANTUM-PULSE demo — seal & unseal live](assets/quantum_pulse_demo.gif)

▶ [Full 26s demo video](assets/quantum_pulse_promo.mp4) &nbsp;·&nbsp; [⭐ Star on GitHub](https://github.com/Naveenub/quantum-pulse)

**`pip install quantum-pulse`  →  `qp seal dataset.json --offline`  →  39× compression + AES-256-GCM**

</div>

---

## Install

```bash
pip install quantum-pulse
```

Or clone for the full server + Docker setup:

```bash
git clone https://github.com/Naveenub/quantum-pulse.git
cd quantum-pulse
cp .env.example .env
docker-compose up -d
```

---

## What Is It?

QUANTUM-PULSE is an open-source **compress-then-encrypt vault** built specifically for LLM training data. Every blob is compressed with a cross-corpus Zstd dictionary, encrypted with AES-256-GCM, integrity-verified with a SHA3-256 Merkle tree, and stored in MongoDB — all through a single API call or CLI command.

### Why not just use gzip / brotli / zstd?

Those tools only compress. QUANTUM-PULSE also:

- **Trains a shared dictionary** across your corpus so every shard benefits from every other shard's patterns
- **Encrypts** each blob with a per-record key derived via PBKDF2 + HKDF
- **Verifies integrity** via SHA3-256 Merkle trees on unseal — silently corrupted data is impossible
- **Groups related shards** into MasterPulses with cross-shard deduplication
- **Exposes a virtual mount** so training scripts read vaulted data without ever decrypting to disk

---

## Why Open Source

Cryptographic tools earn trust through scrutiny, not marketing.

QUANTUM-PULSE is open source because:

1. **Crypto needs public review** — before anyone puts real training data through this pipeline, the implementation should be auditable. Security through obscurity is not security.
2. **Community builds better benchmarks** — ML engineers working with real datasets will find edge cases no synthetic corpus can simulate. Submit yours to [`benchmarks/community/`](benchmarks/community/).
3. **Adoption precedes monetization** — if this solves a real problem at scale, a hosted version becomes a natural next step. Demand should be proven, not assumed.

---

## ☁️ Hosted Version — Coming Soon

Self-hosting a secure vault means managing keys, uptime, and backups yourself. A managed version of QUANTUM-PULSE is in planning:

- **Zero-ops** — no MongoDB to run, no passphrase rotation to script
- **Metered billing** — pay per GiB sealed, not per seat
- **Compliance-ready** — audit log export, key rotation SLA, SOC 2 roadmap
- **Same open protocol** — data sealed via the API can always be unsealed with the self-hosted version; no lock-in

> **Interested in early access?** Open a [GitHub Discussion](../../discussions) or star the repo to signal demand.

---

## Benchmark — 1000 LLM Training Records (1.2 MiB corpus)

```
────────────────────────────────────────────────────────────────────
Algorithm              Ratio      vs gzip     Time      Enc   Int
────────────────────────────────────────────────────────────────────
snappy                 12.03×      −80.9%      0.7 ms    ✗     ✗
lz4                    33.80×      −46.2%      0.4 ms    ✗     ✗
gzip-9                 62.86×      baseline    9.1 ms    ✗     ✗
zstd-L3                76.19×      +21.2%      0.7 ms    ✗     ✗
QUANTUM-PULSE ◀        95.51×      +51.9%    553.4 ms    ✓     ✓   ← fastest secure
zstd-L22+MsgPack       96.60×      +53.7%   1173.4 ms    ✗     ✗
zstd-L22               99.58×      +58.4%   1644.3 ms    ✗     ✗
brotli-11             112.95×      +79.7%   1354.2 ms    ✗     ✗
────────────────────────────────────────────────────────────────────
Enc = AES-256-GCM encryption     Int = SHA3-256 Merkle integrity
```

| Claim | Evidence |
|-------|----------|
| Fastest high-compression pipeline *with security* | 553 ms vs 1173 ms (zstd+mp), 1354 ms (brotli), 1644 ms (zstd-L22) |
| Only option with both encryption + integrity | Every other row shows ✗/✗ |
| 3× faster than zstd-L22 vanilla | Dictionary eliminates per-shard pattern re-discovery |
| Brotli-11 wins raw ratio | 112× vs 95× — but 2.4× slower, no security at all |

Reproduce: `python scripts/benchmark_compare.py`  
Full details: [BENCHMARKS.md](BENCHMARKS.md)

---

## Quick Start

### Offline — no server, no Docker

```bash
pip install quantum-pulse

# Generate a strong passphrase
qp keygen

# Seal a file
qp seal dataset.json --passphrase "yourpassphrase16+" --offline
# → dataset.qp  (AES-256-GCM encrypted · SHA3-256 Merkle signed)

# Recover it — byte-perfect
qp unseal dataset.qp --passphrase "yourpassphrase16+" --offline --output recovered.json

# Benchmark
qp benchmark --passphrase "yourpassphrase16+"
```

### Full server mode — REST API + MongoDB

```bash
git clone https://github.com/Naveenub/quantum-pulse.git
cd quantum-pulse
cp .env.example .env          # set QUANTUM_PASSPHRASE and QUANTUM_API_KEYS
docker-compose up -d

# Seal via API
curl -X POST http://localhost:8747/pulse/seal \
  -H "X-API-Key: my-api-key" \
  -H "Content-Type: application/json" \
  -d '{"payload": {"text": "hello world", "tokens": [1,2,3]}}'

# Seal via CLI
qp seal dataset.json --tag version=v1
qp unseal <pulse-id>
```

### All CLI commands

```bash
qp keygen                                              # generate strong passphrase
qp seal dataset.json --tag v1                          # seal (needs MongoDB)
qp seal dataset.json --passphrase "p16+" --offline     # seal offline → dataset.qp
qp unseal dataset.qp --passphrase "p16+" --offline     # recover offline ← byte-perfect
qp unseal <pulse-id>                                   # decrypt from MongoDB to stdout
qp list                                                # list stored pulses
qp info <pulse-id>                                     # pulse metadata
qp rotate <pulse-id>                                   # re-encrypt under new passphrase
qp scan ./data/                                        # seal entire directory tree
qp master <id1> <id2> ...                              # build cross-shard MasterPulse
qp benchmark --passphrase "p16+"                       # run seal benchmark
qp health                                              # query server health
qp config                                              # print redacted config
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                       QUANTUM-PULSE                          │
│                                                              │
│  Scanner ──▶ Engine (MsgPack→Zstd-dict→AES-GCM→Merkle)      │
│                  │                                           │
│              Vault (PBKDF2+HKDF key derivation)              │
│                  │                                           │
│              DB (MongoDB/GridFS or in-memory)                │
│                                                              │
│  FastAPI :8747 · auth · rate-limit · Prometheus · audit      │
└──────────────────────────────────────────────────────────────┘
```

### Seal Pipeline
```
dict → MsgPack → Zstd-L22+corpus-dict → AES-256-GCM → SHA3-256 Merkle
```

### Wire Format
```
[ MAGIC 4B ][ VER 1B ][ NONCE 12B ][ CIPHERTEXT + GCM-TAG ]
```

---

## Repository Structure

```
quantum-pulse/
├── assets/
│   ├── quantum_pulse_demo.gif            # animated demo — seal & unseal live
│   └── quantum_pulse_promo.mp4           # full 26s promo video
├── benchmarks/
│   └── community/                        # submit your real-world benchmark results here
│       └── README.md
├── core/
│   ├── adaptive.py                       # AdaptiveDictManager — self-improving Zstd dict, retrains every 24h
│   ├── audit.py                          # append-only audit log (JSONL + MongoDB)
│   ├── auth.py                           # API-key + JWT auth, scope-based (read/write/admin)
│   ├── compression.py                    # PulseCompressor — async Zstd-L22 wrapper + streaming
│   ├── config.py                         # Pydantic Settings V2 — all config, secrets, validation
│   ├── db.py                             # async MongoDB / GridFS persistence (motor)
│   ├── engine.py                         # QuantumEngine — MsgPack→Zstd→AES-GCM→Merkle pipeline
│   ├── health.py                         # Kubernetes liveness / readiness / startup probes
│   ├── interface.py                      # FUSE-like virtual mount — sealed files, no plaintext on disk
│   ├── metrics.py                        # Prometheus counters, histograms, gauges
│   ├── middleware.py                     # security headers, request-id, timing, rate-limit
│   ├── retry.py                          # circuit breaker, bulkhead, exponential backoff
│   ├── scanner.py                        # high-speed filesystem scanner (os.scandir + threading)
│   ├── scheduler.py                      # APScheduler — dict retrain, TTL cleanup, metrics flush
│   └── vault.py                          # QuantumVault — PBKDF2-SHA256 + HKDF key derivation
├── models/
│   └── pulse_models.py                   # Pydantic V2 models — PulseBlob, MasterPulse, CompressionStats
├── scripts/
│   ├── benchmark_compare.py              # head-to-head vs snappy / lz4 / gzip / brotli / zstd
│   ├── benchmark_demo.py                 # reproduces README benchmark numbers
│   ├── gen_corpus.py                     # generate synthetic LLM training corpus for testing
│   └── verify_scheduler.py              # verify APScheduler dict retrain fires correctly
├── tests/
│   ├── test_api.py                       # 27 integration tests — full HTTP layer
│   ├── test_engine.py                    # 27 unit tests — core seal/unseal/Merkle pipeline
│   └── test_units.py                     # 223 extended unit tests — 81%+ coverage enforced
├── .github/
│   ├── CODEOWNERS                        # code ownership assignments
│   ├── ISSUE_TEMPLATE/
│   │   ├── benchmark.md                  # community benchmark submission template
│   │   ├── bug_report.md                 # structured bug report
│   │   ├── feature_request.md            # feature proposal
│   │   └── security_audit_issue_template.md  # crypto audit / security review template
│   ├── PULL_REQUEST_TEMPLATE.md          # PR checklist
│   └── workflows/
│       └── ci.yml                        # lint → unit-tests → api-tests → benchmark → docker-build
├── cli.py                                # qp CLI — 12 commands, full offline seal/unseal
├── main.py                               # FastAPI app entry point — all routes wired
├── pyproject.toml                        # build config, dependencies, ruff/mypy/pytest/coverage
├── requirements.txt                      # pinned deps for Docker / CI
├── Makefile                              # make test / bench / lint / docker-up / run
├── Dockerfile
├── docker-compose.yml                    # MongoDB + API, one command start
├── .env.example                          # all environment variables with defaults
├── .pre-commit-config.yaml               # ruff + mypy pre-commit hooks
├── .gitignore
├── BENCHMARKS.md                         # full benchmark methodology and results
├── CHANGELOG.md                          # version history
├── CONTRIBUTING.md                       # contribution guide, design principles
├── CODE_OF_CONDUCT.md
├── LICENSE                               # MIT
└── SECURITY.md                           # vulnerability reporting + audit status
```

---

## Features

**Compression**
- Zstd level 22 with trained cross-corpus dictionary
- MsgPack binary encoding (22% smaller than JSON pre-compression)
- Dictionary auto-retrains every 24h as your corpus grows

**Security**
- AES-256-GCM with hardware AES-NI
- Per-blob HKDF-derived keys — one compromised blob reveals nothing else
- PBKDF2-SHA256, 600,000 iterations (Argon2id planned for v1.1)
- SHA3-256 Merkle tree — every unseal is cryptographically verified
- No formal third-party audit yet — see [SECURITY.md](SECURITY.md)

**Operations**
- FastAPI REST with OpenAPI docs at `/docs`
- API-key + JWT auth, scope-based access (`read`/`write`/`admin`)
- Prometheus metrics, Kubernetes health probes (`/healthz/live|ready|startup`)
- Append-only audit log (JSONL file + MongoDB)
- `qp` CLI with 12 commands, full offline mode

**Developer Experience**
- 277 tests (27 engine unit · 27 API integration · 223 extended unit) — 81%+ coverage
- `make test`, `make bench`, `make lint`, `make docker-up`
- GitHub Actions CI — lint → unit-tests → api-tests → benchmark → docker-build
- Pre-commit hooks — ruff + mypy on every commit

---

## API Reference

| Method | Path | Scope | Description |
|--------|------|-------|-------------|
| POST | `/pulse/seal` | write | Compress + encrypt a payload |
| POST | `/pulse/unseal` | read | Decrypt by pulse ID |
| GET | `/pulse/stream/{id}` | read | Stream unsealed bytes |
| GET | `/pulse/list` | read | List all pulses |
| DELETE | `/pulse/{id}` | admin | Delete a pulse |
| POST | `/pulse/rotate/{id}` | admin | Re-encrypt under new passphrase |
| POST | `/pulse/master` | write | Build a cross-shard MasterPulse |
| POST | `/scan` | write | Seal an entire directory tree |
| GET | `/metrics` | — | Prometheus metrics |
| GET | `/healthz` | — | Full health report |
| GET | `/audit/recent` | admin | Audit log tail |
| POST | `/auth/token` | — | Exchange API key for JWT |

Full interactive docs at `http://localhost:8747/docs`

---

## Configuration

```bash
QUANTUM_PASSPHRASE=my-passphrase          # required, min 16 chars
QUANTUM_API_KEYS=["key1","key2"]          # JSON array
QUANTUM_ENVIRONMENT=development           # development|staging|production
QUANTUM_MONGO_URI=mongodb://localhost:27017
QUANTUM_PORT=8747
QUANTUM_ZSTD_LEVEL=22
QUANTUM_KDF_ITERATIONS=600000
```

Full reference: [`.env.example`](.env.example) · [`core/config.py`](core/config.py)

---

## Testing

```bash
make test        # all 277 tests
make test-unit   # core pipeline only
make test-api    # HTTP layer only
make test-cov    # with coverage report
make bench       # full pipeline benchmark
```

---

## Patch History

| Version | Change |
|---------|--------|
| v1.0.0 | Initial release |
| v1.0.1 | Fixed build backend, added `qp seal --offline`, fixed CI coverage |
| v1.0.2 | Wired `qp unseal --offline` — complete offline round-trip verified |
| v1.0.3 | Published to PyPI — `pip install quantum-pulse` |

---

## Contributing

All contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

**Especially needed:**
- Alternative storage backends (S3, Redis, SQLite)
- Real-world dataset benchmarks → submit to [`benchmarks/community/`](benchmarks/community/)
- Streaming seal/unseal for files > 2 GB
- Language bindings (Rust, Go, Node.js client)
- **Security review** — crypto implementation audit, fuzzing, side-channel analysis

---

## Roadmap

**Open Source**
- [x] PyPI package — `pip install quantum-pulse`
- [ ] Argon2id KDF (replacing PBKDF2-SHA256) — v1.1
- [ ] S3 / GCS storage backend — v1.2
- [ ] Streaming seal for files > 2 GB
- [ ] OpenTelemetry tracing
- [ ] Benchmark vs Apache Parquet + snappy
- [ ] Rust client SDK
- [ ] Key rotation without re-sealing (re-wrap mode)
- [ ] WASM build for browser-side sealing

**Hosted (quantum-pulse.cloud)**
- [ ] Managed API with metered billing (per GiB sealed)
- [ ] Web dashboard — browse, search, audit pulses
- [ ] Team workspaces + role-based access
- [ ] Webhook on seal/unseal events
- [ ] SOC 2 Type II audit

---

## License

[MIT](LICENSE) — free for commercial and personal use.

---

Built on: [python-zstandard](https://github.com/indygreg/python-zstandard) · [cryptography](https://github.com/pyca/cryptography) · [FastAPI](https://github.com/tiangolo/fastapi) · [msgpack-python](https://github.com/msgpack/msgpack-python) · [Pydantic](https://github.com/pydantic/pydantic)
