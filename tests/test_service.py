"""Shared services: miss-budget clamping, backend-aware key check, and
translation of every backend failure into an AskError."""

import asyncio
from dataclasses import dataclass

import httpx
import pytest
from google.genai import errors as genai_errors

from app import service
from app.ai import GenerationError
from app.embeddings import EmbeddingError


@dataclass
class FakeConfig:
    gemini_api_key: str = "test-gemini-key"
    gemini_model: str = "test-model"
    rag_top_k: int = 8
    llm_backend: str = "gemini"
    ollama_url: str = "http://stub:11434"
    ollama_llm_model: str = "test-llm"


def _ask(monkeypatch=None, answer=None, raises=None, config=None, tiddlers=None, **kwargs):
    """Run service.ask_with_tiddlers with answer_question stubbed to return/raise."""
    captured = {}

    async def fake_answer_question(**kw):
        captured.update(kw)
        if raises is not None:
            raise raises
        return answer or {"answer": "ok", "sources": [], "truncated": False}

    if monkeypatch is not None:
        monkeypatch.setattr(service, "answer_question", fake_answer_question)
    result = asyncio.run(
        service.ask_with_tiddlers(
            "q?",
            tiddlers or [],
            config=config or FakeConfig(),
            embedder=object(),
            **kwargs,
        )
    )
    return result, captured


def test_missing_gemini_key():
    with pytest.raises(service.AskError) as exc:
        asyncio.run(
            service.ask_with_tiddlers(
                "q?",
                [],
                config=FakeConfig(gemini_api_key=""),
                embedder=object(),
            )
        )
    assert exc.value.status == 503
    assert "GEMINI_API_KEY" in str(exc.value)


def test_ollama_backend_needs_no_gemini_key(monkeypatch):
    cfg = FakeConfig(gemini_api_key="", llm_backend="ollama")
    result, _ = _ask(monkeypatch, config=cfg)
    assert result["answer"] == "ok"


def test_miss_budget_clamp(monkeypatch):
    _result, captured = _ask(monkeypatch, max_tiddlers=0)
    # max_tiddlers <= 0 must clamp to 1, not disable embedding entirely.
    assert captured["max_embed"] == 1


def test_tiddlers_pass_through(monkeypatch):
    docs = [{"title": "T", "text": "x", "fields": {"tags": "a"}}]
    _result, captured = _ask(monkeypatch, tiddlers=docs)
    assert captured["tiddlers"] is docs


def test_history_passes_through(monkeypatch):
    history = [{"role": "user", "content": "before"}]
    _result, captured = _ask(monkeypatch, history=history)
    assert captured["history"] == history


@pytest.mark.parametrize(
    "raises, status, fragment",
    [
        (EmbeddingError("Ollama may still be starting up."), 503, "Ollama"),
        (GenerationError("The local LLM timed out"), 503, "local LLM"),
        (httpx.ConnectError("boom"), 503, "network issue toward Gemini"),
        (httpx.TimeoutException("slow"), 503, "network issue toward Gemini"),
        (
            genai_errors.ServerError(503, {"error": {"message": "overloaded"}}),
            503,
            "busy",
        ),
        (
            genai_errors.ClientError(400, {"error": {"message": "bad key"}}),
            502,
            "AI model error: bad key",
        ),
    ],
)
def test_backend_failures_translate(monkeypatch, raises, status, fragment):
    with pytest.raises(service.AskError) as exc:
        _ask(monkeypatch, raises=raises)
    assert exc.value.status == status
    assert fragment in str(exc.value)


# --- streaming ---


def _collect(agen):
    async def run():
        return [event async for event in agen]

    return asyncio.run(run())


def test_ask_stream_missing_key_raises_before_stream():
    """Guard failures must come from the coroutine itself, not the generator —
    the route turns them into normal HTTP errors before any bytes go out."""
    with pytest.raises(service.AskError) as exc:
        asyncio.run(
            service.ask_stream_with_tiddlers(
                "q?", [], config=FakeConfig(gemini_api_key=""), embedder=object()
            )
        )
    assert exc.value.status == 503


def test_ask_stream_translates_midstream_failure(monkeypatch):
    async def exploding_stream(**kw):
        yield ("delta", {"text": "par"})
        raise GenerationError("The local LLM timed out")

    monkeypatch.setattr(
        service, "answer_question_stream", lambda **kw: exploding_stream(**kw)
    )
    events = _collect(
        asyncio.run(
            service.ask_stream_with_tiddlers(
                "q?", [], config=FakeConfig(), embedder=object()
            )
        )
    )
    assert events[0] == ("delta", {"text": "par"})
    name, data = events[-1]
    assert name == "error"
    assert data["status"] == 503
    assert "local LLM" in data["message"]


# --- related ---


def test_related_needs_no_generation_backend(monkeypatch):
    """Related is embeddings-only: works with no Gemini key at all."""

    async def fake_related(target, tiddlers, embedder, top_k, max_embed=None):
        assert target["title"] == "T"
        assert top_k == 3
        return [{"title": "Other", "score": 0.9}], False

    monkeypatch.setattr(service, "ai_related", fake_related)
    result = asyncio.run(
        service.related_with_tiddlers(
            {"title": "T", "text": "body", "fields": {}},
            [],
            embedder=object(),
            k=3,
        )
    )
    assert result == {"related": [{"title": "Other", "score": 0.9}], "truncated": False}


def test_related_forwards_target_and_pool(monkeypatch):
    seen = {}

    async def fake_related(target, tiddlers, embedder, top_k, max_embed=None):
        seen["target"], seen["tiddlers"] = target, tiddlers
        return [{"title": "Other", "score": 0.9}], False

    monkeypatch.setattr(service, "ai_related", fake_related)
    target = {"title": "T", "text": "body", "fields": {}}
    pool = [{"title": "Other", "text": "o", "fields": {}}]
    result = asyncio.run(
        service.related_with_tiddlers(target, pool, embedder=object(), k=3)
    )
    assert result == {"related": [{"title": "Other", "score": 0.9}], "truncated": False}
    assert seen["target"] is target
    assert seen["tiddlers"] is pool


def test_related_translates_embedding_failure(monkeypatch):
    async def fake_related(*a, **kw):
        raise EmbeddingError("embedding down")

    monkeypatch.setattr(service, "ai_related", fake_related)
    with pytest.raises(service.AskError) as exc:
        asyncio.run(
            service.related_with_tiddlers(
                {"title": "T", "text": "body", "fields": {}}, [], embedder=object()
            )
        )
    assert exc.value.status == 503


# --- generation commands ---


def test_generate_with_text_runs_command(monkeypatch):
    seen = {}

    async def fake_run_command(command, title, text, cfg, vocabulary=None):
        seen.update(command=command, title=title, text=text, vocabulary=vocabulary)
        return "tag-a\ntag-b"

    monkeypatch.setattr(service, "run_command", fake_run_command)
    result = asyncio.run(
        service.generate_with_text(
            "T", "  body  ", "tags", config=FakeConfig(), vocabulary=["tag-a"]
        )
    )
    assert seen == {"command": "tags", "title": "T", "text": "body", "vocabulary": ["tag-a"]}
    assert result["tags"] == ["tag-a", "tag-b"]


def test_parse_tags_strips_and_caps():
    raw = "- homelab\n* another tag\n\n\"quoted\"\nfive\nsix\nseven"
    assert service._parse_tags(raw) == ["homelab", "another tag", "quoted", "five", "six"]


def test_generate_with_text_guards():
    with pytest.raises(service.AskError) as exc:
        asyncio.run(
            service.generate_with_text("T", "x", "translate", config=FakeConfig())
        )
    assert exc.value.status == 400
    assert "summarize" in str(exc.value)
    with pytest.raises(service.AskError) as exc:
        asyncio.run(
            service.generate_with_text("T", "   ", "summarize", config=FakeConfig())
        )
    assert exc.value.status == 422


def test_generate_with_text_translates_backend_failure(monkeypatch):
    async def fake_run_command(*a, **kw):
        raise GenerationError("The local LLM timed out")

    monkeypatch.setattr(service, "run_command", fake_run_command)
    with pytest.raises(service.AskError) as exc:
        asyncio.run(
            service.generate_with_text("T", "body", "summarize", config=FakeConfig())
        )
    assert exc.value.status == 503
