# quantum-pulse
QUANTUM-PULSE is an open-source compress-then-encrypt vault built specifically for LLM training data. Every blob is compressed with a cross-corpus Zstd dictionary, encrypted with AES-256-GCM, integrity-verified with a SHA3-256 Merkle tree, and stored in MongoDB — all through a single REST API call.
