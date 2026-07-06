"""ai.search: ranked semantic search (retrieve's ranking, snippets, no
generation)."""

import asyncio

from app import ai
from app.ai import search


class FakeEmbedder:
    """Vector [1,0] for texts containing the marker, [0,1] otherwise; queries
    always [1,0]. Texts containing `skip_marker` come back None, as if the
    max_new miss budget skipped them (cache still warming)."""

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
                out.append([1.0, 0.0])
            else:
                out.append([0.0, 1.0])
        return out


def _run(query, tiddlers, top_k=10, embedder=None):
    return asyncio.run(
        search(query, tiddlers, embedder or FakeEmbedder(), top_k=top_k)
    )


def test_returns_scored_results_best_first():
    tiddlers = [
        {"title": "Miss", "text": "nothing to see"},
        {"title": "Hit", "text": "RELEVANT content here"},
    ]
    results, truncated = _run("anything", tiddlers)
    assert results[0]["title"] == "Hit"
    assert results[0]["score"] > 0
    assert "content" in results[0]["snippet"]
    assert truncated is False


def test_zero_score_candidates_dropped():
    """A cosine miss with no keyword overlap is not a search result."""
    tiddlers = [{"title": "Orthogonal", "text": "nothing to see"}]
    results, _ = _run("unrelated question", tiddlers)
    assert results == []


def test_keyword_only_hit_surfaces_without_embedding():
    """A cold (vector=None) note still ranks on an exact keyword match, and the
    request is flagged truncated so the client asks again once warm."""
    tiddlers = [
        {"title": "Other", "text": "unrelated words"},
        {"title": "Wireguard Setup", "text": "SKIPME tunnel notes"},
    ]
    embedder = FakeEmbedder(skip_marker="SKIPME")
    results, truncated = _run("wireguard setup", tiddlers, embedder=embedder)
    assert [r["title"] for r in results] == ["Wireguard Setup"]
    assert truncated is True


def test_top_k_limits_results():
    tiddlers = [
        {"title": "A", "text": "RELEVANT one"},
        {"title": "B", "text": "RELEVANT two"},
        {"title": "C", "text": "RELEVANT three"},
    ]
    results, _ = _run("q", tiddlers, top_k=2)
    assert len(results) == 2


def test_empty_candidates():
    assert _run("q", [{"title": "Blank", "text": "  "}]) == ([], False)


def test_snippet_is_short_with_ellipsis_for_long_notes():
    long_text = "RELEVANT " + ("filler words " * 60)  # well past the snippet budget
    results, _ = _run("q", [{"title": "Long", "text": long_text}])
    snippet = results[0]["snippet"]
    assert len(snippet) <= ai._SNIPPET_BUDGET + 2  # +2 for the ellipsis chars
    assert snippet.endswith("…")
