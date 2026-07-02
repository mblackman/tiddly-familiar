"""Pure helpers in app.ai: context assembly, budgets, chunking, keywords."""

import pytest

from app import ai
from app.ai import (
    _build_context,
    _chunk_text,
    _embed_text,
    _excerpt,
    _keyword_score,
    _query_terms,
)


def test_build_context_formats_and_lists_sources():
    tiddlers = [
        {"title": "Note A", "text": "Alpha content."},
        {"title": "Note B", "text": "  Beta content.  "},
    ]
    context, sources = _build_context(tiddlers)
    assert context == "## Note A\nAlpha content.\n\n## Note B\nBeta content."
    assert sources == ["Note A", "Note B"]


def test_build_context_skips_empty_text():
    tiddlers = [
        {"title": "Empty", "text": "   "},
        {"title": "Textless"},
        {"title": "Real", "text": "content"},
    ]
    context, sources = _build_context(tiddlers)
    assert sources == ["Real"]
    assert "Empty" not in context and "Textless" not in context


def test_build_context_title_from_nested_fields():
    tiddlers = [{"fields": {"title": "Nested"}, "text": "body"}]
    context, sources = _build_context(tiddlers)
    assert context == "## Nested\nbody"
    assert sources == ["Nested"]


def test_build_context_total_budget_drops_tail(monkeypatch):
    """Input is ranked best-first, so overflow drops the least relevant notes
    — never the first one, even if it alone exceeds the budget."""
    monkeypatch.setattr(ai, "_TOTAL_CONTEXT_BUDGET", 10)
    tiddlers = [
        {"title": "Best", "text": "x" * 50},
        {"title": "Worst", "text": "y"},
    ]
    context, sources = _build_context(tiddlers)
    assert sources == ["Best"]
    assert "Worst" not in context


def test_embed_text_prepends_title():
    assert _embed_text("T", "body") == "T\nbody"
    # Title carries signal even with no text; no stray whitespace either way.
    assert _embed_text("Only Title", "") == "Only Title"


def test_chunk_text_short_is_single_chunk():
    # Must return the text byte-identical so short tiddlers keep the cache
    # hashes they had before chunking existed.
    assert _chunk_text("short text") == [(0, "short text")]


def test_chunk_text_overlaps_and_covers():
    text = "".join(chr(ord("a") + i % 26) for i in range(ai._CHUNK_SIZE * 2 + 500))
    chunks = _chunk_text(text)
    step = ai._CHUNK_SIZE - ai._CHUNK_OVERLAP
    assert [off for off, _c in chunks] == [0, step, 2 * step]
    # Consecutive chunks share the overlap region.
    assert chunks[0][1][-ai._CHUNK_OVERLAP:] == chunks[1][1][: ai._CHUNK_OVERLAP]
    # The final chunk reaches the end of the text: full coverage.
    off, last = chunks[-1]
    assert off + len(last) == len(text)


def test_query_terms_drop_stopwords_and_short_tokens():
    assert _query_terms("What is the Caddy reverse proxy for?") == {
        "caddy", "reverse", "proxy",
    }
    assert _query_terms("what is it") == set()


def test_keyword_score_weights_title_and_tags():
    terms = {"caddy", "proxy"}
    title_hit = {"title": "Caddy", "fields": {}, "text": "nothing relevant"}
    tag_hit = {"title": "Infra", "fields": {"tags": "proxy homelab"}, "text": ""}
    body_hit = {"title": "Infra", "fields": {}, "text": "we use caddy here"}
    miss = {"title": "Recipes", "fields": {}, "text": "flour and water"}
    assert _keyword_score(terms, title_hit) == 0.5   # one head hit of two terms
    assert _keyword_score(terms, tag_hit) == 0.5
    assert _keyword_score(terms, body_hit) == 0.25   # body hits worth half
    assert _keyword_score(terms, miss) == 0.0
    assert _keyword_score(set(), title_hit) == 0.0


def test_excerpt_short_text_untouched():
    assert _excerpt("tiny", [(0, "tiny", 1.0)]) == "tiny"


def test_excerpt_picks_best_chunks_in_document_order(monkeypatch):
    monkeypatch.setattr(ai, "_TIDDLER_CONTEXT_BUDGET", 8)
    text = "aaaabbbbccccdddd"
    chunks = [
        (0, "aaaa", 0.9),
        (4, "bbbb", 0.1),
        (8, "cccc", 0.2),
        (12, "dddd", 0.95),
    ]
    # Budget fits the two best chunks; they come back in document order.
    assert _excerpt(text, chunks) == "aaaa\n[...]\ndddd"


def test_excerpt_merges_overlapping_chunks(monkeypatch):
    monkeypatch.setattr(ai, "_TIDDLER_CONTEXT_BUDGET", 8)
    text = "aaaabbbbccccdddd"
    chunks = [
        (4, "bbbbcc", 0.9),
        (8, "ccccdd", 0.8),
        (0, "aaaa", 0.1),
    ]
    # The two winners overlap on "cc" — merged into one continuous span.
    assert _excerpt(text, chunks) == "bbbbccccdd"
