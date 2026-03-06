# Changelog

All notable changes to QUANTUM-PULSE are documented here.  
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) · Versioning: [SemVer](https://semver.org/)

---

## [1.0.0] — 2026-03-06

Initial open-source release.

### Added
- **Core pipeline** — MsgPack → Zstd-L22+dict → AES-256-GCM → SHA3-256 Merkle
- **QuantumEngine** — seal/unseal/rotate with per-blob HKDF-derived keys
- **QuantumVault** — PBKDF2-SHA256 (600k iterations) + HKDF sub-key derivation, 300s cache
- **AdaptiveDictManager** — self-improving Zstd dictionary, A/B versioning, auto-retrain every 24h
- **PulseCompressor** — async Zstd wrapper, streaming compress/decompress, benchmark runner
- **FastAPI REST API** — seal, unseal, stream, list, rotate, master-build, dict-train endpoints
- **Authentication** — API-key + JWT, scope-based (`read` / `write` / `admin`)
- **Rate limiting** — SlowAPI with per-key limits
- **MongoDB backend** — motor async driver + in-memory fallback
- **Virtual mount filesystem** — FUSE-like interface (`core/interface.py`)
- **Prometheus metrics** — counters, histograms, gauges for all operations
- **Health probes** — `/healthz/live`, `/healthz/ready`, `/healthz/startup` (Kubernetes-ready)
- **Append-only audit log** — JSONL file + MongoDB, tamper-evident
- **Circuit breaker + bulkhead** — `core/retry.py` with tenacity + asyncio semaphore
- **APScheduler background jobs** — dict retraining, TTL cleanup, metrics flush
- **`qp` CLI** — 12 commands: seal, unseal, scan, rotate, list, info, master, keygen, benchmark, health, config
- **Docker + docker-compose** — MongoDB + API, one-command start
- **CI/CD** — GitHub Actions: lint → unit-tests → API-tests → benchmark → docker-build
- **277 tests** — 27 engine unit, 27 API integration, 223 extended unit (82%+ coverage)
- **Benchmarks** — 95.51× compression with full AES-256-GCM + SHA3-256 Merkle, fastest secure pipeline

### Security
- AES-256-GCM with hardware AES-NI acceleration
- Per-blob nonces — nonce reuse is impossible
- PBKDF2-SHA256 at 600,000 iterations (NIST SP 800-132 compliant)
- SHA3-256 Merkle tree — every unseal is cryptographically verified
- No partial decryption — authentication failure raises immediately

---

## [Unreleased]

### Planned
- S3 / GCS storage backend
- Streaming seal for files > 2 GB
- OpenTelemetry tracing
- Rust client SDK
- Key rotation without re-sealing (re-wrap mode)
- Hosted managed API (quantum-pulse.cloud)
