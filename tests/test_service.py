"""Shared ask service: default filter, miss-budget clamping, and translation of
every backend failure into an AskError with a user-facing message."""

import asyncio
from dataclasses import dataclass, field

import httpx
import pytest
from google.genai import errors as genai_errors

from app import service
from app.embeddings import EmbeddingError


@dataclass
class FakeConfig:
    gemini_api_key: str = "test-gemini-key"
    gemini_model: str = "test-model"
    rag_top_k: int = 8


@dataclass
class FakeNotebook:
    tiddlers: list = field(default_factory=list)
    seen_filters: list = field(default_factory=list)

    async def filter_tiddlers(self, filter, full=False):
        self.seen_filters.append(filter)
        return self.tiddlers


def _ask(nbm, monkeypatch=None, answer=None, raises=None, **kwargs):
    """Run service.ask with answer_question stubbed to return/raise."""
    captured = {}

    async def fake_answer_question(**kw):
        captured.update(kw)
        if raises is not None:
            raise raises
        return answer or {"answer": "ok", "sources": [], "truncated": False}

    if monkeypatch is not None:
        monkeypatch.setattr(service, "answer_question", fake_answer_question)
    result = asyncio.run(
        service.ask(nbm, "q?", config=FakeConfig(), embedder=object(), **kwargs)
    )
    return result, captured


def test_missing_gemini_key():
    with pytest.raises(service.AskError) as exc:
        asyncio.run(
            service.ask(
                FakeNotebook(),
                "q?",
                config=FakeConfig(gemini_api_key=""),
                embedder=object(),
            )
        )
    assert exc.value.status == 503
    assert "GEMINI_API_KEY" in str(exc.value)


def test_default_filter_and_clamp(monkeypatch):
    nbm = FakeNotebook()
    _result, captured = _ask(nbm, monkeypatch, max_tiddlers=0)
    assert nbm.seen_filters == [service.DEFAULT_FILTER]
    # MCP args aren't range-validated; <=0 must clamp to 1, not disable embedding.
    assert captured["max_embed"] == 1


def test_explicit_filter_passes_through(monkeypatch):
    nbm = FakeNotebook()
    _ask(nbm, monkeypatch, filter="[tag[project]]")
    assert nbm.seen_filters == ["[tag[project]]"]


@pytest.mark.parametrize(
    "raises, status, fragment",
    [
        (EmbeddingError("Ollama may still be starting up."), 503, "Ollama"),
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
        _ask(FakeNotebook(), monkeypatch, raises=raises)
    assert exc.value.status == status
    assert fragment in str(exc.value)
