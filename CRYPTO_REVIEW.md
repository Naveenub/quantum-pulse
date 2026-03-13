# QUANTUM-PULSE — Cryptographic Review Guide

This file gives any reviewer everything needed to assess the cryptographic implementation in one place. No formal audit exists yet. The goal of this document is to make an informal review as fast as possible.

**Relevant source files (total ~330 lines of crypto code):**

| File | Lines | What it covers |
|------|-------|----------------|
| `core/engine.py` | 219 | AES-256-GCM seal/unseal, Merkle tree, wire format, VaultKey |
| `core/vault.py` | 113 | PBKDF2-SHA256 master key, HKDF sub-key derivation, key rotation |

Everything else (compression, API, scheduler, CLI) is not cryptographic. You can ignore it.

---

## What the Pipeline Does

```
Plaintext object
  → MsgPack serialise
  → Zstd-L22 compress (with trained corpus dictionary)
  → AES-256-GCM encrypt  (sub-key derived per-blob via HKDF)
  → SHA3-256 Merkle tree (over ciphertext)
  → Wire blob stored to MongoDB or .qp file
```

Unseal reverses all five steps. Merkle verification happens before any plaintext is returned.

---

## Key Derivation — `core/engine.py` + `core/vault.py`

### Master key (PBKDF2)

```python
# core/engine.py — VaultKey.__init__
AES_KEY_BYTES   = 32          # 256-bit output
KDF_ITERATIONS  = 600_000     # OWASP 2024 recommendation
KDF_SALT_BYTES  = 32          # fresh os.urandom(32) per vault init

kdf = PBKDF2HMAC(
    algorithm=hashes.SHA256(),
    length=AES_KEY_BYTES,       # 32 bytes → AES-256
    salt=self._salt,            # random 32-byte salt, stored with blob meta
    iterations=KDF_ITERATIONS,  # 600k
)
self._key = kdf.derive(passphrase.encode("utf-8"))
```

**Questions for reviewers:**
- Is PBKDF2-SHA256 at 600k iterations adequate for the stated threat model (LLM training data, not passwords to banking)?
- Salt is stored in `PulseBlob.salt` (hex). Any concerns with how the salt is persisted alongside the ciphertext?
- Argon2id migration is planned for v1.1 — is this upgrade path safe without breaking existing sealed blobs?

---

### Per-blob sub-key (HKDF)

```python
# core/vault.py — QuantumVault._sync_hkdf
HKDF_INFO_PREFIX = b"quantum-pulse-shard-key-v1:"

hkdf = HKDF(
    algorithm=hashes.SHA256(),
    length=AES_KEY_BYTES,                   # 32 bytes
    salt=blob_salt,                          # the blob's nonce is used as HKDF salt
    info=HKDF_INFO_PREFIX + pulse_id.encode(),
)
sub_key = hkdf.derive(master_key)
```

**Questions for reviewers:**
- HKDF `info` parameter is `b"quantum-pulse-shard-key-v1:" + pulse_id.encode()`. Is this sufficient binding?
- HKDF `salt` is the blob's AES-GCM nonce (12 bytes). Is reusing the nonce as HKDF salt a problem?
- Does per-blob key isolation via HKDF actually hold — does compromise of one blob's sub-key reveal anything about the master key or other sub-keys?

---

## Encryption — `core/engine.py`

### Wire format

```
[ MAGIC 4B "QPLS" ][ VER 1B ][ NONCE 12B ][ CIPHERTEXT + GCM-TAG 16B ]
```

Header is 17 bytes. Everything after byte 17 is the AES-256-GCM ciphertext + authentication tag.

### Seal

```python
# core/engine.py — QuantumEngine.seal
AES_NONCE_BYTES = 12   # 96-bit GCM nonce

nonce = os.urandom(AES_NONCE_BYTES)          # fresh per seal
sub_key = await vault.derive_shard_key(pulse_id, salt)
aad = b"QUANTUM-PULSE-v1"                    # associated authenticated data

ciphertext = AESGCM(sub_key).encrypt(nonce, plaintext, aad)
blob = _pack_header(nonce) + ciphertext
```

### Unseal

```python
# core/engine.py — QuantumEngine.unseal
_ver, nonce = _unpack_header(blob)
ciphertext = blob[HEADER_SIZE:]              # everything after 17-byte header
sub_key = await vault.derive_shard_key(pulse_id, bytes.fromhex(meta.salt))

# AESGCM.decrypt raises InvalidTag if ciphertext or AAD was tampered with
plaintext = AESGCM(sub_key).decrypt(nonce, ciphertext, aad)
```

**Questions for reviewers:**
- 96-bit random nonce with AES-256-GCM — at expected scale (millions of blobs per vault), is collision probability a concern?
- `aad = b"QUANTUM-PULSE-v1"` is a fixed string. Is this sufficient as AAD, or should it include more context (e.g. pulse_id)?
- Is there any concern with caching `AESGCM` objects via `@lru_cache(maxsize=32)` keyed by `key_hex`?

---

## Integrity — `core/engine.py`

### Merkle tree construction

```python
# core/engine.py — build_merkle_tree
def build_merkle_tree(leaves: Sequence[bytes]) -> tuple[list[str], str]:
    # Leaf hashes
    layer = [hashlib.sha3_256(leaf).digest() for leaf in leaves]

    # Build tree bottom-up
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])          # duplicate last node if odd count
        next_layer = [
            hashlib.sha3_256(layer[i] + layer[i + 1]).digest()
            for i in range(0, len(layer), 2)
        ]
        layer = next_layer

    return all_nodes_hex, root_hex
```

Merkle root is stored in `PulseBlob.merkle_root`. On every unseal, the root is recomputed from the decrypted ciphertext chunks and compared before any plaintext is returned.

**Questions for reviewers:**
- Odd-leaf duplication (`layer[-1]` repeated) — does this create a second-preimage vulnerability?
- Merkle tree is over **ciphertext** (not plaintext) — encrypt-then-MAC pattern. Is this the right layer to verify at?
- For single-shard blobs (the common case), the Merkle tree is a single SHA3-256 hash. Is that degenerate case sufficient, or does it change the security properties?

---

## Key Cache — `core/vault.py`

```python
# core/vault.py — _KeyCache
KEY_CACHE_TTL_SECONDS = 300   # 5-minute TTL

# Keyed by (master_key_hex[:16], pulse_id)
# Evicted after TTL or on vault.lock()
```

Sub-keys are cached in memory for 5 minutes to avoid re-running HKDF on hot read paths. The cache is a plain Python dict in process memory.

**Questions for reviewers:**
- Any concerns with caching derived sub-keys in a plain dict? (No zeroing on eviction.)
- Cache key uses first 16 hex chars of master key — is this a timing oracle risk?

---

## What Is NOT Novel

To be explicit: there is no novel cryptography here. The implementation uses:

- `cryptography` library (PyCA) for all primitives — PBKDF2, HKDF, AES-256-GCM
- `hashlib.sha3_256` from the Python standard library for Merkle
- `os.urandom` for all randomness

The only design decisions are: which primitives to chain, in which order, with which parameters. A review confirming "these are the right primitives assembled correctly" is the full scope of what's needed.

---

## How to Run It

```bash
git clone https://github.com/Naveenob/quantum-pulse
cd quantum-pulse
pip install -e ".[dev]"

# Run crypto-specific tests
pytest tests/test_engine.py -v               # 27 unit tests — seal/unseal/Merkle

# Run the full benchmark to exercise the pipeline end-to-end
qp benchmark --passphrase "yourpassphrase16+"

# Offline round-trip
echo '{"text": "test", "tokens": [1,2,3]}' > test.json
qp seal test.json --passphrase "yourpassphrase16+" --offline
qp unseal test.qp --passphrase "yourpassphrase16+" --offline --output recovered.json
diff test.json recovered.json  # should be identical
```

---

## Known Gaps (Already Disclosed)

These are documented in `README.md`, `SECURITY.md`, and every GitHub release:

1. **No formal third-party audit** — this document is a step toward one
2. **PBKDF2-SHA256 over Argon2id** — Argon2id provides better memory-hardness; migration planned for v1.1
3. **Not recommended for highly sensitive production data** until a formal review exists

---

## Vulnerability Reporting

Security issues: see [SECURITY.md](SECURITY.md). Private reporting is set up via GitHub Security Advisories.

Public discussion: [GitHub Discussions](../../discussions) — community review thread.
