"""
Dependency-light smoke tests for the RAG retrieval layer. No live Ollama/Gemini
or Playwright needed — the Ollama call is stubbed. Run: pytest tests/test_smoke.py
"""

import asyncio

from app.embeddings import Embedder, rank


def test_rank_orders_by_cosine():
    # query points along x; candidate 1 is aligned, candidate 0 is orthogonal,
    # candidate 2 is opposite. Expected order: 1 (best), 0, 2 (worst).
    query = [1.0, 0.0]
    candidates = [[0.0, 1.0], [2.0, 0.0], [-1.0, 0.0]]
    order = [idx for idx, _score in rank(query, candidates)]
    assert order[0] == 1
    assert order[-1] == 2


def test_rank_empty_candidates():
    assert rank([1.0, 0.0], []) == []


def test_rank_handles_zero_vector():
    # A zero (empty-text) candidate must not blow up with NaN — it just ranks last.
    order = [idx for idx, _score in rank([1.0, 0.0], [[1.0, 0.0], [0.0, 0.0]])]
    assert order[0] == 0


def _stub_embedder():
    """Embedder whose raw call is replaced by a deterministic counter. Each unique
    text maps to a stable 2-vector; call log records the batches actually sent."""
    emb = Embedder("http://stub", "test-model")
    calls: list[list[str]] = []

    async def fake_raw(texts):
        calls.append(list(texts))
        # Deterministic vector from text length so results are stable.
        return [[float(len(t)), 1.0] for t in texts]

    emb._embed_raw = fake_raw  # type: ignore[assignment]
    return emb, calls


def test_embed_documents_cache_hit():
    emb, calls = _stub_embedder()

    async def run():
        docs = ["alpha", "beta", "gamma"]
        first = await emb.embed_documents(docs)
        second = await emb.embed_documents(docs)  # identical → fully cached
        return first, second

    first, second = asyncio.run(run())
    assert first == second
    # Only the first call should have hit the raw embedder.
    assert len(calls) == 1
    assert calls[0] == ["alpha", "beta", "gamma"]


def test_embed_documents_invalidates_on_change():
    emb, calls = _stub_embedder()

    async def run():
        await emb.embed_documents(["alpha", "beta"])
        # "beta" -> "beta!" changes its hash; "alpha" stays cached.
        await emb.embed_documents(["alpha", "beta!"])

    asyncio.run(run())
    assert calls[0] == ["alpha", "beta"]      # first: both embedded
    assert calls[1] == ["beta!"]              # second: only the changed one


def test_embed_documents_max_new_budget():
    """max_new bounds misses per call; skipped docs come back None and are
    picked up by later calls, so coverage converges."""
    emb, calls = _stub_embedder()

    async def run():
        docs = ["alpha", "beta", "gamma"]
        first = await emb.embed_documents(docs, max_new=2)
        second = await emb.embed_documents(docs, max_new=2)
        return first, second

    first, second = asyncio.run(run())
    assert first[0] is not None and first[1] is not None
    assert first[2] is None                   # over budget on the cold call
    assert all(v is not None for v in second)  # second call finishes the job
    assert calls[0] == ["alpha", "beta"]
    assert calls[1] == ["gamma"]              # only the leftover miss


def test_embed_documents_max_new_ignores_cache_hits():
    """The budget applies to misses only — cached docs never count against it."""
    emb, calls = _stub_embedder()

    async def run():
        await emb.embed_documents(["alpha", "beta"])
        return await emb.embed_documents(["alpha", "beta", "gamma"], max_new=1)

    vecs = asyncio.run(run())
    assert all(v is not None for v in vecs)
    assert calls[1] == ["gamma"]
