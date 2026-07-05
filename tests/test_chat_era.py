"""Milestone 5 ("chat era") ai-layer tests: streaming answers, chat history
threading, and one-shot generation commands. (Service-level wiring for these
lives in test_service.py.)"""

import asyncio
import json
from dataclasses import dataclass

import httpx
import pytest

from app import ai
from app.ai import (
    GenerationError,
    _gemini_contents,
    _ollama_messages,
    _stream_ollama,
    _trim_history,
    answer_question_stream,
    run_command,
)


@dataclass
class FakeConfig:
    gemini_api_key: str = "test-gemini-key"
    gemini_model: str = "test-model"
    rag_top_k: int = 8
    llm_backend: str = "gemini"
    ollama_url: str = "http://stub:11434"
    ollama_llm_model: str = "test-llm"


class FakeEmbedder:
    async def embed_query(self, text):
        return [1.0, 0.0]

    async def embed_documents(self, texts, max_new=None):
        return [[1.0, 0.0] for _ in texts]


# --- history trimming and message assembly ---


def test_trim_history_drops_invalid_and_caps_turns():
    history = [{"role": "user", "content": f"turn {i}"} for i in range(20)]
    history.insert(0, {"role": "system", "content": "not a chat role"})
    history.insert(0, {"role": "user", "content": ""})
    kept = _trim_history(history)
    assert len(kept) == ai._MAX_HISTORY_TURNS
    assert kept[-1]["content"] == "turn 19"


def test_trim_history_char_budget_keeps_newest(monkeypatch):
    monkeypatch.setattr(ai, "_HISTORY_CHAR_BUDGET", 10)
    history = [
        {"role": "user", "content": "x" * 8},
        {"role": "assistant", "content": "y" * 8},
    ]
    kept = _trim_history(history)
    # Budget fits only one turn — the newest survives, never the oldest.
    assert [t["content"][0] for t in kept] == ["y"]


def test_trim_history_newest_turn_survives_even_over_budget(monkeypatch):
    monkeypatch.setattr(ai, "_HISTORY_CHAR_BUDGET", 5)
    kept = _trim_history([{"role": "user", "content": "x" * 100}])
    assert len(kept) == 1


def test_ollama_messages_order():
    msgs = _ollama_messages(
        "SYS",
        "the prompt",
        [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ],
    )
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("system", "SYS"),
        ("user", "q1"),
        ("assistant", "a1"),
        ("user", "the prompt"),
    ]


def test_gemini_contents_maps_assistant_to_model():
    contents = _gemini_contents(
        "the prompt", [{"role": "assistant", "content": "a1"}]
    )
    assert [c.role for c in contents] == ["model", "user"]
    assert contents[-1].parts[0].text == "the prompt"


def test_history_reaches_ollama_payload(monkeypatch):
    """End to end through answer_question: prior turns must land in the
    /api/chat messages between system and the final prompt."""
    seen = {}

    async def fake_ollama(prompt, url, model, system=None, history=None):
        seen["messages"] = _ollama_messages(system, prompt, history)
        return "fine"

    monkeypatch.setattr(ai, "_generate_ollama", fake_ollama)
    asyncio.run(
        ai.answer_question(
            "follow-up?",
            [{"title": "Note", "text": "content"}],
            FakeEmbedder(),
            FakeConfig(llm_backend="ollama"),
            history=[{"role": "user", "content": "first question"}],
        )
    )
    roles = [m["role"] for m in seen["messages"]]
    assert roles == ["system", "user", "user"]
    assert seen["messages"][1]["content"] == "first question"


# --- streaming ---


def _collect(agen):
    async def run():
        return [event async for event in agen]

    return asyncio.run(run())


def test_answer_question_stream_deltas_then_done(monkeypatch):
    async def fake_stream(prompt, api_key, model, system=None, history=None):
        for text in ["Hello ", "[[Note]]", " world"]:
            yield text

    monkeypatch.setattr(ai, "_stream_gemini", fake_stream)
    events = _collect(
        answer_question_stream(
            "q?", [{"title": "Note", "text": "content"}], FakeEmbedder(), FakeConfig()
        )
    )
    assert [name for name, _ in events] == ["delta", "delta", "delta", "done"]
    done = events[-1][1]
    assert done["answer"] == "Hello [[Note]] world"
    # Sources come from citations in the assembled stream.
    assert done["sources"] == ["Note"]
    assert done["truncated"] is False


def test_answer_question_stream_no_candidates():
    events = _collect(
        answer_question_stream("q?", [], FakeEmbedder(), FakeConfig())
    )
    assert [name for name, _ in events] == ["done"]
    assert events[0][1]["sources"] == []


def _fake_stream_client(monkeypatch, lines=None, status=200):
    """Stub httpx.AsyncClient.stream() yielding the given NDJSON lines."""

    class FakeResp:
        status_code = status

        async def aread(self):
            return b""

        def raise_for_status(self):
            if status >= 400:
                raise httpx.HTTPStatusError(
                    "err",
                    request=httpx.Request("POST", "http://stub"),
                    response=httpx.Response(status, request=httpx.Request("POST", "http://stub")),
                )

        async def aiter_lines(self):
            for line in lines or []:
                yield line

    class FakeStreamCM:
        async def __aenter__(self):
            return FakeResp()

        async def __aexit__(self, *a):
            return False

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, json=None):
            return FakeStreamCM()

    monkeypatch.setattr(ai.httpx, "AsyncClient", FakeClient)


def test_stream_ollama_parses_ndjson(monkeypatch):
    _fake_stream_client(
        monkeypatch,
        lines=[
            json.dumps({"message": {"content": "Hel"}, "done": False}),
            "",
            json.dumps({"message": {"content": "lo"}, "done": False}),
            json.dumps({"message": {"content": ""}, "done": True}),
        ],
    )

    async def run():
        return [t async for t in _stream_ollama("p", "http://stub", "m")]

    assert asyncio.run(run()) == ["Hel", "lo"]


def test_stream_ollama_missing_model_is_friendly(monkeypatch):
    _fake_stream_client(monkeypatch, status=404)

    async def run():
        return [t async for t in _stream_ollama("p", "http://stub", "tiny-llm")]

    with pytest.raises(GenerationError) as exc:
        asyncio.run(run())
    assert "tiny-llm" in str(exc.value)


def test_stream_ollama_inline_error_line(monkeypatch):
    _fake_stream_client(monkeypatch, lines=[json.dumps({"error": "boom"})])

    async def run():
        return [t async for t in _stream_ollama("p", "http://stub", "m")]

    with pytest.raises(GenerationError) as exc:
        asyncio.run(run())
    assert "boom" in str(exc.value)


# --- generation commands ---


def test_run_command_formats_prompt(monkeypatch):
    seen = {}

    async def fake_generate_text(system, prompt, cfg, history=None):
        seen["system"] = system
        seen["prompt"] = prompt
        return "  a summary  "

    monkeypatch.setattr(ai, "_generate_text", fake_generate_text)
    result = asyncio.run(run_command("summarize", "My Note", "the text", FakeConfig()))
    assert result == "a summary"
    assert "My Note" in seen["prompt"]
    assert "the text" in seen["prompt"]


def test_run_command_tags_includes_vocabulary(monkeypatch):
    seen = {}

    async def fake_generate_text(system, prompt, cfg, history=None):
        seen["prompt"] = prompt
        return "tag1"

    monkeypatch.setattr(ai, "_generate_text", fake_generate_text)
    asyncio.run(
        run_command("tags", "T", "text", FakeConfig(), vocabulary=["alpha", "beta"])
    )
    assert "alpha, beta" in seen["prompt"]
