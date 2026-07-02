"""Shared test helpers: a stubbed Embedder factory (no live Ollama needed)."""

import pytest

from app.embeddings import Embedder


@pytest.fixture
def stub_embedder():
    """Factory for Embedders whose raw Ollama call is a deterministic stub.

    Returns (make, calls): `make(cache_path=None)` builds a stubbed Embedder;
    `calls` logs every batch sent to the fake backend across all instances.
    Each text maps to a stable 2-vector derived from its length, and both
    components are small integers so float32 round-trips are exact.
    """
    calls: list[list[str]] = []

    def make(cache_path: str | None = None) -> Embedder:
        emb = Embedder("http://stub", "test-model", cache_path=cache_path)

        async def fake_raw(texts):
            calls.append(list(texts))
            return [[float(len(t)), 1.0] for t in texts]

        emb._embed_raw = fake_raw  # type: ignore[assignment]
        return emb

    return make, calls
