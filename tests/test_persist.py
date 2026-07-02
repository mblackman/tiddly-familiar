"""Persistent embedding cache: vectors survive process restarts via SQLite."""

import asyncio

from app.embeddings import _EMBED_BATCH_SIZE, Embedder


def test_cache_survives_restart(tmp_path, stub_embedder):
    make, calls = stub_embedder
    db = str(tmp_path / "embeddings.sqlite3")
    docs = ["alpha", "beta", "gamma"]

    async def run():
        first = make(cache_path=db)
        vecs_before = await first.embed_documents(docs)
        await first.aclose()
        # "Restart": a fresh instance pointed at the same file.
        second = make(cache_path=db)
        vecs_after = await second.embed_documents(docs)
        await second.aclose()
        return vecs_before, vecs_after

    vecs_before, vecs_after = asyncio.run(run())
    assert vecs_before == vecs_after
    # Only the first instance's cold pass hit the backend.
    assert calls == [["alpha", "beta", "gamma"]]


def test_cache_is_scoped_to_model(tmp_path, stub_embedder):
    """Switching embed models must not serve vectors computed by the old one."""
    make, calls = stub_embedder
    db = str(tmp_path / "embeddings.sqlite3")

    async def run():
        first = make(cache_path=db)  # model "test-model"
        await first.embed_documents(["alpha"])
        await first.aclose()

        other = Embedder("http://stub", "other-model", cache_path=db)

        async def fake_raw(texts):
            calls.append(list(texts))
            return [[float(len(t)), 1.0] for t in texts]

        other._embed_raw = fake_raw  # type: ignore[assignment]
        await other.embed_documents(["alpha"])
        await other.aclose()

    asyncio.run(run())
    # Embedded once per model — the second model's call was a cache miss.
    assert calls == [["alpha"], ["alpha"]]


def test_partial_progress_persists_across_restart(tmp_path, stub_embedder):
    """A failure mid-corpus keeps completed chunks on disk, so after a restart
    the retry only re-sends what's still missing."""
    make, calls = stub_embedder
    db = str(tmp_path / "embeddings.sqlite3")
    docs = [f"doc-{i:03d}" for i in range(_EMBED_BATCH_SIZE + 3)]

    async def run():
        first = make(cache_path=db)
        original_raw = first._embed_raw
        attempts = 0

        async def flaky_raw(texts):
            nonlocal attempts
            attempts += 1
            if attempts == 2:  # second chunk of the cold pass blows up
                raise RuntimeError("ollama fell over")
            return await original_raw(texts)

        first._embed_raw = flaky_raw  # type: ignore[assignment]
        try:
            await first.embed_documents(docs)
        except RuntimeError:
            pass
        await first.aclose()

        second = make(cache_path=db)
        vecs = await second.embed_documents(docs)
        await second.aclose()
        return vecs

    vecs = asyncio.run(run())
    assert all(v is not None for v in vecs)
    # Cold pass persisted its first chunk; the post-restart retry only sent
    # the 3 leftovers.
    assert [len(c) for c in calls] == [_EMBED_BATCH_SIZE, 3]


def test_no_cache_path_stays_in_memory(tmp_path, stub_embedder):
    """Without cache_path nothing persists — a fresh instance re-embeds."""
    make, calls = stub_embedder

    async def run():
        first = make()
        await first.embed_documents(["alpha"])
        await first.aclose()
        second = make()
        await second.embed_documents(["alpha"])
        await second.aclose()

    asyncio.run(run())
    assert calls == [["alpha"], ["alpha"]]
