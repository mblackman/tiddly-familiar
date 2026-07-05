"""EmbedPrewarmer: background embedding fills the same cache keys ask-time
retrieval reads, and failures/overflow never take the worker down."""

import asyncio

from app.ai import _chunk_text, _embed_text
from app.embeddings import _MAX_EMBED_CHARS, _hash
from app.prewarm import EmbedPrewarmer


def _expected_keys(title: str, text: str) -> list[str]:
    """The (model-scoped) cache keys retrieval would look up for this note."""
    return [
        _hash(_embed_text(title, chunk)[:_MAX_EMBED_CHARS])
        for _offset, chunk in _chunk_text(text)
    ]


def test_prewarm_fills_ask_time_cache_keys(stub_embedder):
    make, calls = stub_embedder
    short = {"title": "Zebra", "text": "A zebra is a striped horse.", "fields": {}}
    long = {"title": "Long", "text": "x" * 5000, "fields": {}}

    async def run():
        emb = make()
        warmer = EmbedPrewarmer(emb)
        warmer.start()
        warmer.enqueue([short, long])
        await warmer.drain()
        await warmer.aclose()
        return emb

    emb = asyncio.run(run())
    for note in (short, long):
        for key in _expected_keys(note["title"], note["text"]):
            assert key in emb._cache
    # the long note really was chunked (3 windows for 5000 chars)
    assert len(_expected_keys("Long", "x" * 5000)) == 3
    assert calls  # the stub backend was actually hit


def test_worker_survives_embedding_failure(stub_embedder):
    make, _calls = stub_embedder
    note_a = {"title": "A", "text": "first", "fields": {}}
    note_b = {"title": "B", "text": "second", "fields": {}}

    async def run():
        emb = make()
        real_raw = emb._embed_raw
        fail = {"on": True}

        async def flaky_raw(texts):
            if fail["on"]:
                raise RuntimeError("ollama exploded")
            return await real_raw(texts)

        emb._embed_raw = flaky_raw
        warmer = EmbedPrewarmer(emb)
        warmer.start()
        warmer.enqueue([note_a])
        await warmer.drain()  # batch fails, worker must survive
        fail["on"] = False
        warmer.enqueue([note_b])
        await warmer.drain()
        await warmer.aclose()
        return emb

    emb = asyncio.run(run())
    assert _expected_keys("A", "first")[0] not in emb._cache
    assert _expected_keys("B", "second")[0] in emb._cache


def test_enqueue_dedups_and_skips_empty(stub_embedder):
    make, _calls = stub_embedder
    warmer = EmbedPrewarmer(make())
    note = {"title": "T", "text": "body", "fields": {}}
    warmer.enqueue([note])
    warmer.enqueue([note])  # same content while still pending: not re-queued
    warmer.enqueue([{"title": "Empty", "text": "  ", "fields": {}}])
    assert warmer._queue.qsize() == 1


def test_queue_overflow_drops_without_raising(stub_embedder):
    make, _calls = stub_embedder
    warmer = EmbedPrewarmer(make(), queue_max=2)
    notes = [{"title": f"N{i}", "text": f"b{i}", "fields": {}} for i in range(5)]
    warmer.enqueue(notes)
    assert warmer._queue.qsize() == 2
