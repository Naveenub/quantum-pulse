# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.x (latest) | ✅ Yes |

## Reporting a Vulnerability

**Please do NOT open a public GitHub Issue for security vulnerabilities.**

### Option A — GitHub Private Reporting (preferred)
Go to **Security → Report a vulnerability** in this repository.

### Option B — Email
Email the maintainers directly with:
- Description of the vulnerability
- Steps to reproduce
- Impact assessment
- Any suggested fix

You will receive an acknowledgement within 48 hours and a resolution timeline within 7 days.

## Scope

In scope:
- Cryptographic weaknesses (key derivation, encryption, Merkle verification)
- Authentication bypass in the REST API
- Data leakage or integrity failures
- Remote code execution via API inputs

Out of scope:
- Issues in upstream dependencies (report those upstream)
- Theoretical attacks with no practical exploit

## Disclosure Policy

We follow coordinated disclosure. Once a fix is ready we will:
1. Release a patched version
2. Publish a GitHub Security Advisory
3. Credit the reporter (unless they prefer anonymity)

## Cryptographic Review Status

**No formal third-party audit has been conducted.**

This is disclosed in the README, all release notes, and every post about this project. It means QUANTUM-PULSE is not yet recommended for protecting highly sensitive production data.

### What exists instead

- **CRYPTO_REVIEW.md** — a single file documenting all cryptographic code, exact parameters, and specific open questions: [`CRYPTO_REVIEW.md`](CRYPTO_REVIEW.md)
- **Community review thread** — open GitHub Discussion inviting public cryptographic review: [Community Crypto Review — QUANTUM-PULSE v1.0.3](https://github.com/Naveenub/quantum-pulse/discussions/5#discussion-9642706)
- **Standard primitives only** — PBKDF2-SHA256, HKDF, AES-256-GCM, SHA3-256 via PyCA `cryptography` library. No hand-rolled crypto.

### Known gaps

| Gap | Status |
|-----|--------|
| Formal third-party audit | Not yet — planned for v1.1 |
| Argon2id KDF | PBKDF2-SHA256 (600k iters) used now — Argon2id migration planned for v1.1 |
| Production recommendation | Not yet recommended for sensitive data until formal review complete |

If you are a cryptographer and willing to review the ~330 lines of crypto code, please see [`CRYPTO_REVIEW.md`](CRYPTO_REVIEW.md).
