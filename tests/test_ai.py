"""Pure helpers in app.ai: context assembly, budgets, chunking, keywords,
and the local-LLM generation backend."""

import asyncio
import types

import httpx
import pytest

from app import ai
from app.ai import (
    GenerationError,
    _build_context,
    _chunk_text,
    _embed_text,
    _excerpt,
    _generate_ollama,
    _keyword_score,
    _query_terms,
    _rewrite_query,
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


# --- history-aware query rewrite ---


HISTORY = [
    {"role": "user", "content": "How is Wireguard configured?"},
    {"role": "assistant", "content": "Via the wg0 interface on the router."},
]


def test_rewrite_query_no_history_skips_generation(monkeypatch):
    """No prior turns → the question is already standalone; no LLM call."""
    called = False

    async def fake_generate(system, prompt, cfg, history=None):
        nonlocal called
        called = True
        return "SHOULD NOT BE USED"

    monkeypatch.setattr(ai, "_generate_text", fake_generate)
    cfg = types.SimpleNamespace(query_rewrite=True)
    out = asyncio.run(_rewrite_query("what is it?", [], cfg))
    assert out == "what is it?"
    assert called is False


def test_rewrite_query_disabled_returns_question(monkeypatch):
    async def fake_generate(system, prompt, cfg, history=None):
        raise AssertionError("rewrite disabled: must not generate")

    monkeypatch.setattr(ai, "_generate_text", fake_generate)
    cfg = types.SimpleNamespace(query_rewrite=False)
    assert asyncio.run(_rewrite_query("what about it?", HISTORY, cfg)) == "what about it?"


def test_rewrite_query_folds_history(monkeypatch):
    seen = {}

    async def fake_generate(system, prompt, cfg, history=None):
        seen["prompt"] = prompt
        return "  How is Wireguard configured on the router?  "

    monkeypatch.setattr(ai, "_generate_text", fake_generate)
    cfg = types.SimpleNamespace(query_rewrite=True)
    out = asyncio.run(_rewrite_query("what about the router?", HISTORY, cfg))
    assert out == "How is Wireguard configured on the router?"  # stripped
    assert "Wireguard" in seen["prompt"] and "what about the router?" in seen["prompt"]


def test_rewrite_query_failure_falls_back_to_question(monkeypatch):
    async def fake_generate(system, prompt, cfg, history=None):
        raise RuntimeError("model down")

    monkeypatch.setattr(ai, "_generate_text", fake_generate)
    cfg = types.SimpleNamespace(query_rewrite=True)
    assert asyncio.run(_rewrite_query("what about it?", HISTORY, cfg)) == "what about it?"


def test_answer_question_retrieves_on_rewrite_generates_on_original(monkeypatch):
    """Retrieval must rank by the rewritten query; generation must still see
    the user's original question."""
    seen = {}

    async def fake_rewrite(question, history, cfg):
        return "STANDALONE QUERY"

    async def fake_retrieve(query, tiddlers, embedder, top_k, max_embed=None):
        seen["retrieval_query"] = query
        return [{"title": "N", "text": "body"}], False

    async def fake_generate(question, selected, cfg, history=None):
        seen["generation_question"] = question
        return {"answer": "a", "sources": []}

    monkeypatch.setattr(ai, "_rewrite_query", fake_rewrite)
    monkeypatch.setattr(ai, "retrieve", fake_retrieve)
    monkeypatch.setattr(ai, "_generate", fake_generate)
    cfg = types.SimpleNamespace(rag_top_k=8, query_rewrite=True)

    out = asyncio.run(
        ai.answer_question("original?", [{"title": "N", "text": "body"}], object(), cfg)
    )
    assert seen["retrieval_query"] == "STANDALONE QUERY"
    assert seen["generation_question"] == "original?"
    assert out["truncated"] is False


# --- Ollama generation backend ---


def _patch_ollama(monkeypatch, respond):
    """Replace httpx.AsyncClient in ai.py with a stub whose post() is `respond`."""

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return await respond(url, json)

    monkeypatch.setattr(ai.httpx, "AsyncClient", FakeClient)


def test_generate_ollama_happy_path(monkeypatch):
    seen = {}

    async def respond(url, payload):
        seen["url"] = url
        seen["payload"] = payload
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": "local answer"}},
            request=httpx.Request("POST", url),
        )

    _patch_ollama(monkeypatch, respond)
    answer = asyncio.run(_generate_ollama("prompt", "http://ollama:11434/", "m"))
    assert answer == "local answer"
    assert seen["url"] == "http://ollama:11434/api/chat"
    assert seen["payload"]["stream"] is False
    assert [m["role"] for m in seen["payload"]["messages"]] == ["system", "user"]


def test_generate_ollama_missing_model_is_friendly(monkeypatch):
    async def respond(url, payload):
        return httpx.Response(404, request=httpx.Request("POST", url))

    _patch_ollama(monkeypatch, respond)
    with pytest.raises(GenerationError) as exc:
        asyncio.run(_generate_ollama("prompt", "http://ollama:11434", "tiny-llm"))
    assert "tiny-llm" in str(exc.value)


def test_generate_ollama_connect_error_is_friendly(monkeypatch):
    async def respond(url, payload):
        raise httpx.ConnectError("refused")

    _patch_ollama(monkeypatch, respond)
    with pytest.raises(GenerationError) as exc:
        asyncio.run(_generate_ollama("prompt", "http://ollama:11434", "m"))
    assert "starting up" in str(exc.value)
