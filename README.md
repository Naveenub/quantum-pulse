# ⚡ QUANTUM-PULSE

> **Extreme-density encrypted data vault for LLM training pipelines.**  
> MsgPack + Zstd-L22 + corpus dictionary + AES-256-GCM + SHA3-256 Merkle trees + REST API

[![CI](https://github.com/Naveenub/quantum-pulse/actions/workflows/ci.yml/badge.svg)](https://github.com/Naveenub/quantum-pulse/actions)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-54%20passing-brightgreen.svg)](#testing)

---

## What Is It?

QUANTUM-PULSE is an open-source **compress-then-encrypt vault** built specifically for LLM training data. Every blob is compressed with a cross-corpus Zstd dictionary, encrypted with AES-256-GCM, integrity-verified with a SHA3-256 Merkle tree, and stored in MongoDB — all through a single REST API call.

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

**Honest summary:**

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

```bash
# 1. Clone and install
git clone https://github.com/Naveenub/quantum-pulse.git
cd quantum-pulse
pip install -r requirements.txt

# 2. Set your passphrase (min 16 chars)
export QUANTUM_PASSPHRASE="my-strong-passphrase-here"
export QUANTUM_API_KEYS='["my-api-key"]'

# 3. Start (no MongoDB needed — uses in-memory storage by default)
uvicorn main:app --port 8747

# 4. Seal your first blob
curl -X POST http://localhost:8747/pulse/seal \
  -H "X-API-Key: my-api-key" \
  -H "Content-Type: application/json" \
  -d '{"payload": {"text": "hello world", "tokens": [1,2,3]}}'

# 5. Unseal it
curl -X POST http://localhost:8747/pulse/unseal \
  -H "X-API-Key: my-api-key" \
  -H "Content-Type: application/json" \
  -d '{"pulse_id": "<id from step 4>"}'
```

**Or via the CLI:**

```bash
qp keygen                          # generate strong passphrase
qp seal dataset.json --tag v1      # seal a file
qp list                            # list all pulses
qp unseal <pulse-id>               # decrypt to stdout
qp health                          # check server status
```

---

## Docker (One Command)

```bash
docker-compose up -d
curl http://localhost:8747/healthz
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
├── .env.example                        # all environment variables with defaults
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   ├── benchmark.md                # community benchmark submission template
│   │   ├── bug_report.md               # structured bug report template
│   │   └── feature_request.md          # feature proposal template
│   ├── PULL_REQUEST_TEMPLATE.md        # PR checklist
│   └── workflows/
│       └── ci.yml                      # GitHub Actions: lint → test → bench → docker
├── .gitignore
├── BENCHMARKS.md                       # full benchmark results vs gzip/lz4/brotli/zstd
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md                     # contribution guide, design principles, structure
├── Dockerfile
├── LICENSE                             # MIT
├── Makefile                            # make test / bench / lint / docker-up / run
├── README.md
├── SECURITY.md                         # responsible disclosure policy
├── benchmarks/
│   └── community/                      # submit your benchmark results here
│       └── README.md
├── cli.py                              # qp CLI (seal / unseal / scan / rotate / health …)
├── core/
│   ├── adaptive.py                     # AdaptiveDictManager — self-improving dict, A/B versioning
│   ├── audit.py                        # append-only audit log (JSONL + MongoDB)
│   ├── auth.py                         # API key + JWT authentication, scope-based access
│   ├── compression.py                  # PulseCompressor — async Zstd wrapper + streaming
│   ├── config.py                       # Pydantic Settings V2 — all config, secrets, validation
│   ├── db.py                           # MongoDB / in-memory storage backend
│   ├── engine.py                       # QuantumEngine — MsgPack→Zstd→AES-GCM→Merkle pipeline
│   ├── health.py                       # liveness / readiness / startup probes
│   ├── interface.py                    # virtual mount filesystem (FUSE-like)
│   ├── metrics.py                      # Prometheus counters, histograms, gauges
│   ├── middleware.py                   # HTTP stack: CORS, security headers, RFC 7807 errors
│   ├── retry.py                        # circuit breaker, bulkhead, exponential backoff
│   ├── scanner.py                      # async directory scanner → seal pipeline
│   ├── scheduler.py                    # APScheduler background jobs
│   └── vault.py                        # PBKDF2 + HKDF key derivation, rotation
├── docker-compose.yml                  # MongoDB + QUANTUM-PULSE, one command
├── main.py                             # FastAPI app — all endpoints wired together
├── models/
│   └── pulse_models.py                 # Pydantic V2 models: PulseBlob, MasterPulse, …
├── pyproject.toml                      # project metadata, ruff, mypy, coverage config
├── requirements.txt
├── scripts/
│   ├── benchmark_compare.py            # head-to-head vs snappy/lz4/gzip/brotli/zstd
│   ├── benchmark_demo.py               # full seal/unseal/Merkle pipeline benchmark
│   └── gen_corpus.py                   # reproducible LLM training corpus generator
└── tests/
    ├── test_api.py                     # 27 FastAPI integration tests (full HTTP stack)
    ├── test_engine.py                  # 27 unit tests (core compression/crypto pipeline)
    └── test_units.py                   # 223 unit tests (auth, middleware, vault, scheduler, …)
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
- PBKDF2-SHA256, 600,000 iterations
- SHA3-256 Merkle tree — every unseal is cryptographically verified

**Operations**
- FastAPI REST with OpenAPI docs at `/docs`
- API-key + JWT auth, scope-based access (`read`/`write`/`admin`)
- Prometheus metrics, Kubernetes health probes (`/healthz/live|ready|startup`)
- Append-only audit log (JSONL file + MongoDB)
- `qp` CLI with 12 commands

**Developer Experience**
- 277 tests (223 unit · 27 engine · 27 API integration)
- `make test`, `make bench`, `make lint`, `make docker-up`
- GitHub Actions CI out of the box

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
make test        # all 54 tests
make test-unit   # core pipeline only
make test-api    # HTTP layer only
make test-cov    # with coverage report
make bench       # full pipeline benchmark
```

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
- [ ] S3 / GCS storage backend
- [ ] Streaming seal for files > 2 GB
- [ ] OpenTelemetry tracing
- [ ] Benchmark vs Apache Parquet + snappy
- [ ] WASM build for browser-side sealing
- [ ] Rust client SDK
- [ ] Key rotation without re-sealing (re-wrap mode)

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
