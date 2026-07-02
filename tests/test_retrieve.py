"""Hybrid chunked retrieval (ai.retrieve): cosine + keyword blend, chunk-level
ranking of long tiddlers, and cache-warming (None-vector) behaviour."""

import asyncio

from app import ai
from app.ai import retrieve


class FakeEmbedder:
    """Duck-typed embedder: vector [1,0] for texts containing a marker,
    [0,1] otherwise; queries always [1,0]. Texts containing `skip_marker`
    come back None, as if the max_new miss budget skipped them."""

    def __init__(self, marker="RELEVANT", skip_marker=None):
        self.marker = marker
        self.skip_marker = skip_marker

    async def embed_query(self, text):
        return [1.0, 0.0]

    async def embed_documents(self, texts, max_new=None):
        out = []
        for t in texts:
            if self.skip_marker and self.skip_marker in t:
                out.append(None)
            elif self.marker in t:
                out.append([1.0, 0.0])  # cosine 1.0 vs the query
            else:
                out.append([0.0, 1.0])  # cosine 0.0
        return out


def _run(question, tiddlers, top_k=2, embedder=None):
    return asyncio.run(
        retrieve(question, tiddlers, embedder or FakeEmbedder(), top_k=top_k)
    )


def test_cosine_relevance_selects_and_orders():
    tiddlers = [
        {"title": "Noise", "text": "nothing to see"},
        {"title": "Hit", "text": "RELEVANT content here"},
    ]
    selected, truncated = _run("question words", tiddlers, top_k=1)
    assert [s["title"] for s in selected] == ["Hit"]
    assert truncated is False


def test_empty_candidates():
    assert _run("q", [{"title": "Blank", "text": "  "}]) == ([], False)


def test_keyword_match_beats_close_cosine():
    """Both docs are cosine-identical misses; the exact title match must win
    on the keyword bonus."""
    tiddlers = [
        {"title": "Grocery List", "text": "milk and eggs"},
        {"title": "Wireguard Setup", "text": "notes about the tunnel"},
    ]
    selected, _ = _run("how is wireguard configured?", tiddlers, top_k=1)
    assert selected[0]["title"] == "Wireguard Setup"


def test_unembedded_keyword_hit_still_surfaces():
    """While the cache is warming (vector=None) a keyword match must still be
    retrievable — previously unembedded docs were dropped entirely."""
    tiddlers = [
        {"title": "Other", "text": "unrelated words"},
        {"title": "Caddy Proxy", "text": "SKIPME reverse proxy config"},
    ]
    embedder = FakeEmbedder(skip_marker="SKIPME")
    selected, truncated = _run(
        "caddy proxy setup", tiddlers, top_k=1, embedder=embedder
    )
    assert selected[0]["title"] == "Caddy Proxy"
    assert truncated is True


def test_long_tiddler_found_by_tail_chunk(monkeypatch):
    """The relevant text sits past the first chunk boundary — chunk-level
    scoring must find it, and the excerpt must contain the tail, not just
    the head of the document."""
    monkeypatch.setattr(ai, "_CHUNK_SIZE", 100)
    monkeypatch.setattr(ai, "_CHUNK_OVERLAP", 10)
    monkeypatch.setattr(ai, "_TIDDLER_CONTEXT_BUDGET", 150)
    long_text = ("filler " * 40) + "the RELEVANT protocol details"  # tail-only
    tiddlers = [
        {"title": "Short Noise", "text": "boring"},
        {"title": "Long Doc", "text": long_text},
    ]
    selected, _ = _run("q", tiddlers, top_k=1)
    assert selected[0]["title"] == "Long Doc"
    assert "RELEVANT protocol details" in selected[0]["text"]
    # Budgeted excerpt, not the whole document.
    assert len(selected[0]["text"]) < len(long_text)


def test_short_tiddler_embed_text_matches_prechunking_format():
    """Short docs must produce exactly one embed text, `title\\ntext`.strip() —
    the content-hash format the persistent cache was built with."""
    seen = []

    class RecordingEmbedder(FakeEmbedder):
        async def embed_documents(self, texts, max_new=None):
            seen.extend(texts)
            return await super().embed_documents(texts, max_new=max_new)

    tiddlers = [{"title": "Note", "text": "short body"}]
    _run("q", tiddlers, top_k=1, embedder=RecordingEmbedder())
    assert seen == ["Note\nshort body"]
