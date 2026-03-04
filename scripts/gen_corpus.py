#!/usr/bin/env python3
"""
Generate a reproducible LLM training corpus for benchmarking.

Usage:
    python scripts/gen_corpus.py                    # 500 records → corpus.jsonl
    python scripts/gen_corpus.py --records 2000     # larger corpus
    python scripts/gen_corpus.py --seed 99          # different random seed
"""
import argparse, json, random

TEXTS = [
    "The transformer architecture revolutionized natural language processing through self-attention.",
    "Positional encodings allow sequence models to reason about token order without recurrence.",
    "Layer normalization stabilizes training by normalizing activations within each layer.",
    "Feed-forward blocks apply non-linear transformations after each attention step.",
    "Residual connections enable gradient flow through very deep neural networks.",
    "Pre-training on large corpora transfers general knowledge to downstream tasks.",
    "Fine-tuning adapts pretrained weights to specific tasks with small labelled datasets.",
    "Tokenization splits raw text into sub-word units using byte-pair encoding.",
    "Beam search decodes sequences by keeping the k most probable partial outputs.",
    "Temperature scaling controls the sharpness of the output probability distribution.",
]

def gen(n: int, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    records = []
    for i in range(n):
        records.append({
            "id":       f"doc_{i:06d}",
            "text":     " ".join(rng.choices(TEXTS, k=rng.randint(2, 6))),
            "tokens":   list(range(rng.randint(32, 256))),
            "metadata": {
                "source":    rng.choice(["arxiv", "books", "web", "code"]),
                "year":      rng.randint(2019, 2024),
                "citations": rng.randint(0, 500),
                "split":     "train" if i % 5 != 0 else "val",
            },
        })
    return records

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--records", type=int, default=500)
    p.add_argument("--seed",    type=int, default=42)
    p.add_argument("--output",  type=str, default="corpus.jsonl")
    args = p.parse_args()

    records = gen(args.records, args.seed)
    with open(args.output, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    size = sum(len(json.dumps(r)) for r in records)
    print(f"Wrote {args.records} records → {args.output}  ({size/1024:.1f} KiB)")
