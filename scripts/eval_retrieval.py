#!/usr/bin/env python3
"""Offline retrieval-quality eval against a LIVE embedding model (Ollama).

Runs the shared corpus/queries (tests/eval/*.json) through app.ai.retrieve with
a real Embedder and prints recall@k / MRR / per-query hits. Use it when tuning
the embed model, chunk size, or the cosine/keyword blend — the CI guardrail
(tests/test_eval_retrieval.py) only checks a deterministic lexical baseline and
can't see synonym matching.

    OLLAMA_URL=http://localhost:11434 EMBED_MODEL=nomic-embed-text \
        .venv/bin/python scripts/eval_retrieval.py --top-k 5 --fail-under 0.8
"""

import argparse
import asyncio
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests", "eval"))

from app.embeddings import Embedder  # noqa: E402
from harness import evaluate, load_corpus, load_queries  # noqa: E402


def _print_table(report: dict, model: str) -> None:
    print(
        f"model={model}  top_k={report['top_k']}  "
        f"recall@k={report['recall_at_k']:.3f}  MRR={report['mrr']:.3f}\n"
    )
    for r in report["rows"]:
        rank = "-"
        if r["hit"]:
            rank = str(next(i for i, t in enumerate(r["ranked"], 1) if t in r["expected"]))
        mark = "✓" if r["hit"] else "✗"
        print(f" {mark} rank={rank:>2}  {r['question']}")
        if not r["hit"]:
            print(f"        expected {r['expected']}, got {r['ranked']}")


async def _run(args) -> dict:
    embedder = Embedder(args.ollama_url, args.embed_model)
    try:
        return await evaluate(embedder, load_corpus(), load_queries(), top_k=args.top_k)
    finally:
        await embedder.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument(
        "--fail-under", type=float, default=0.0,
        help="exit non-zero if recall@k falls below this (for CI/tuning gates)",
    )
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--embed-model", default=os.environ.get("EMBED_MODEL", "nomic-embed-text"))
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    args = ap.parse_args()

    report = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_table(report, args.embed_model)

    if report["recall_at_k"] < args.fail_under:
        sys.exit(1)


if __name__ == "__main__":
    main()
