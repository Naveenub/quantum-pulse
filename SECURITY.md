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
Send a **private** email to: **security@quantum-pulse.dev** (or open a GitHub private report if that address bounces) with:
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
