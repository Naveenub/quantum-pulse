# Contributing to QUANTUM-PULSE

Thank you for your interest! All contributions are welcome — bug fixes, new features,
performance improvements, documentation, and benchmark results.

---

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/quantum-pulse
cd quantum-pulse
pip install -r requirements.txt
pip install lz4 brotli python-snappy  # for benchmarks

# Run tests before you change anything
make test

# Run benchmarks
python scripts/benchmark_compare.py
```

---

## Ways to Contribute

### 🐛 Bug Reports
Open a GitHub Issue with:
- Python version (`python --version`)
- OS and architecture
- Minimal reproduction script
- Full traceback

### 💡 Feature Requests
Open an Issue with the `enhancement` label. Describe:
- The use case (who benefits and how)
- Proposed API / interface
- Any tradeoffs you foresee

### 🔧 Pull Requests

1. Fork the repo and create a branch: `git checkout -b feat/my-improvement`
2. Write code and tests (`tests/test_engine.py` for core, `tests/test_api.py` for API)
3. Run the full suite: `make test`
4. Run lint: `make lint`
5. Open a PR against `main` — describe what changed and why

**PR checklist:**
- [ ] All 277 tests pass (`make test`)
- [ ] No new lint errors (`make lint`)
- [ ] New behaviour is tested
- [ ] BENCHMARKS.md updated if performance changed

### 📊 Benchmark Submissions
Run the comparison on your hardware or a real dataset and submit results:

```bash
python scripts/benchmark_compare.py --records 1000 --output results.json
```

Open a PR adding `benchmarks/community/YOUR_DESCRIPTION.json` plus a short markdown
note with your corpus type and hardware specs.

---

## Project Structure

```
core/
  adaptive.py     # AdaptiveDictManager — self-improving dict, A/B versioning
  config.py       # Pydantic Settings — all configuration
  engine.py       # Compression + encryption pipeline (central)
  vault.py        # Key derivation, rotation, HKDF
  compression.py  # Zstd dictionary training and benchmarking
  scanner.py      # Async filesystem scanner
  db.py           # MongoDB / in-memory storage
  interface.py    # Virtual mount (FUSE-like)
  auth.py         # API key + JWT authentication
  middleware.py   # HTTP middleware stack
  metrics.py      # Prometheus metrics
  health.py       # Liveness / readiness probes
  audit.py        # Append-only audit log
  retry.py        # Circuit breaker, bulkhead, retry
  scheduler.py    # Background jobs (APScheduler)

models/
  pulse_models.py # All Pydantic V2 models

tests/
  test_engine.py  # 27 unit tests (core pipeline)
  test_api.py     # 27 integration tests (HTTP layer)
  test_units.py   # 223 unit tests (auth, middleware, vault, scheduler, …)

scripts/
  benchmark_compare.py  # Head-to-head vs competitors
  benchmark_demo.py     # Full pipeline benchmark

cli.py            # Typer CLI (qp seal / unseal / scan / rotate …)
main.py           # FastAPI entry point
```

---

## Design Principles

1. **Correctness over speed** — the crypto must be right. AES-256-GCM with fresh
   nonces, PBKDF2-SHA256 at 600k iterations, SHA3-256 Merkle trees. No shortcuts.

2. **Async-first** — all I/O is awaitable. CPU-bound work runs in
   `ThreadPoolExecutor` to keep the event loop responsive.

3. **Fail loudly on integrity errors** — if a ciphertext doesn't authenticate,
   raise immediately. Never return partial decrypted data.

4. **Observable by default** — every operation emits Prometheus metrics and an
   audit log record. Operators should never need to guess what happened.

5. **Honest benchmarks** — we don't cherry-pick data to inflate ratios. The
   benchmark corpus is realistic LLM training data and the competitor comparison
   is fair.

---

## Testing Philosophy

- Unit tests in `tests/test_engine.py` test the core pipeline in isolation.
- Integration tests in `tests/test_api.py` test the full HTTP stack with a real
  in-process app (no mocks for the engine itself).
- A PR should not reduce test coverage. Add tests for every new code path.

---

## Security Issues

Please **do not** open a public GitHub Issue for security vulnerabilities.
Email the maintainers directly or use GitHub's private vulnerability reporting:
**Security → Report a vulnerability** in the repo settings.

---

## Licence

By submitting a PR you agree that your contribution will be licensed under the
same [MIT licence](LICENSE) as the rest of the project.
