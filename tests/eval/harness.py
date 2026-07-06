"""Shared retrieval-eval logic: corpus/query loaders, a deterministic
network-free embedder, and metric computation over app.ai.retrieve.

Two consumers share this module:
- tests/test_eval_retrieval.py — CI guardrail with the HashingEmbedder (no
  Ollama), asserting recall@k / MRR stay above a floor so scoring-logic
  regressions surface deterministically.
- scripts/eval_retrieval.py — offline quality run against a live embed model,
  for tuning the model, chunk size, or the cosine/keyword blend.
"""

import hashlib
import json
import os
import re

import numpy as np

from app.ai import retrieve

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
_TOKEN = re.compile(r"[a-z0-9]+")
_HASH_DIM = 256


def load_corpus() -> list[dict]:
    """The eval corpus as retrieval-shaped tiddler dicts ({title, text,
    fields.tags})."""
    with open(os.path.join(EVAL_DIR, "corpus.json"), encoding="utf-8") as f:
        raw = json.load(f)
    return [
        {"title": n["title"], "text": n["text"], "fields": {"tags": n.get("tags", "")}}
        for n in raw
    ]


def load_queries() -> list[dict]:
    with open(os.path.join(EVAL_DIR, "queries.json"), encoding="utf-8") as f:
        return json.load(f)


class HashingEmbedder:
    """Deterministic, network-free stand-in for the Ollama embedder:
    L2-normalized hashed term-frequency vectors, so cosine ≈ lexical overlap.

    Enough to keep retrieval *ranking* reproducible in CI; it cannot model
    synonymy, so real semantic quality belongs to the offline tier against a
    live embed model. Implements the subset of Embedder that retrieve() uses.
    """

    def __init__(self, dim: int = _HASH_DIM):
        self._dim = dim

    def _vec(self, text: str) -> list[float]:
        v = np.zeros(self._dim, dtype=np.float32)
        for tok in _TOKEN.findall(text.lower()):
            bucket = int(hashlib.sha1(tok.encode("utf-8")).hexdigest(), 16) % self._dim
            v[bucket] += 1.0
        norm = float(np.linalg.norm(v))
        return (v / norm).tolist() if norm else v.tolist()

    async def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    async def embed_documents(self, texts: list[str], max_new=None) -> list[list[float]]:
        return [self._vec(t) for t in texts]


def _reciprocal_rank(ranked: list[str], expected: list[str]) -> float:
    for i, title in enumerate(ranked, 1):
        if title in expected:
            return 1.0 / i
    return 0.0


async def evaluate(embedder, corpus: list[dict], queries: list[dict], top_k: int = 5) -> dict:
    """Run every query through app.ai.retrieve and score it. Returns a report
    {rows, recall_at_k, mrr, top_k} where each row records the ranked titles,
    whether an expected note was in the top-k, and its reciprocal rank."""
    rows = []
    for q in queries:
        selected, _truncated = await retrieve(
            q["question"], corpus, embedder, top_k=top_k
        )
        ranked = [s["title"] for s in selected]
        expected = q["expected"]
        rows.append(
            {
                "question": q["question"],
                "expected": expected,
                "ranked": ranked,
                "hit": any(t in expected for t in ranked),
                "rr": _reciprocal_rank(ranked, expected),
            }
        )
    n = len(rows) or 1
    return {
        "rows": rows,
        "recall_at_k": sum(r["hit"] for r in rows) / n,
        "mrr": sum(r["rr"] for r in rows) / n,
        "top_k": top_k,
    }
