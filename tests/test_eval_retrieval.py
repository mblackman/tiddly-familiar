"""CI retrieval guardrail: run the shared eval corpus/queries through
app.ai.retrieve with a deterministic (network-free) embedder and assert
recall@k / MRR stay above a floor. A regression in chunking, the cosine/keyword
blend, or top-k selection trips these before it ships. Real semantic quality is
the offline tier — scripts/eval_retrieval.py against a live embed model."""

import asyncio
import os
import sys

# tests/eval is not on sys.path under pytest's default prepend import mode;
# add it so `harness` imports directly (repo root is already importable for app).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "eval"))

from harness import HashingEmbedder, evaluate, load_corpus, load_queries  # noqa: E402


def test_retrieval_recall_and_mrr_above_floor():
    corpus, queries = load_corpus(), load_queries()
    report = asyncio.run(evaluate(HashingEmbedder(), corpus, queries, top_k=5))
    assert report["recall_at_k"] >= 0.9, report
    assert report["mrr"] >= 0.75, report


def test_every_expected_title_exists_in_corpus():
    """Guard the fixtures themselves: a typo'd expected title would make the
    metrics silently unreachable."""
    titles = {t["title"] for t in load_corpus()}
    for q in load_queries():
        for expected in q["expected"]:
            assert expected in titles, f"unknown expected title: {expected!r}"
