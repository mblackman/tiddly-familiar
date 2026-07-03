"""Milestone 5 ("chat era"): streaming answers, chat history threading,
one-shot generation commands, and synthesis digests."""

import asyncio
import json
from dataclasses import dataclass, field

import httpx
import pytest

from app import ai, service
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
    digest_filter: str = "[days:modified[-7]]"


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


# --- service.ask_stream ---


@dataclass
class FakeNotebook:
    tiddlers: list = field(default_factory=list)
    store: dict = field(default_factory=dict)
    rendered: dict = field(default_factory=dict)
    seen_filters: list = field(default_factory=list)
    written: list = field(default_factory=list)

    async def filter_tiddlers(self, filter, full=False):
        self.seen_filters.append(filter)
        if filter == "[tags[]]":
            return ["existing-tag", "another tag"]
        return self.tiddlers

    async def get_tiddler(self, title):
        return self.store.get(title)

    async def put_tiddler(self, title, fields, text):
        self.written.append((title, fields, text))
        return True

    async def render(self, title, mode="plain"):
        return self.rendered.get(title, "")


def test_ask_stream_guard_raises_before_streaming():
    with pytest.raises(service.AskError) as exc:
        asyncio.run(
            service.ask_stream(
                FakeNotebook(),
                "q?",
                config=FakeConfig(gemini_api_key=""),
                embedder=object(),
            )
        )
    assert "GEMINI_API_KEY" in str(exc.value)


def test_ask_stream_translates_midstream_failure(monkeypatch):
    async def exploding_stream(**kw):
        yield ("delta", {"text": "par"})
        raise GenerationError("The local LLM timed out")

    monkeypatch.setattr(
        service, "answer_question_stream", lambda **kw: exploding_stream(**kw)
    )
    events = _collect(
        asyncio.run(
            service.ask_stream(
                FakeNotebook(), "q?", config=FakeConfig(), embedder=object()
            )
        )
    )
    assert events[0] == ("delta", {"text": "par"})
    name, data = events[-1]
    assert name == "error"
    assert data["status"] == 503
    assert "local LLM" in data["message"]


def test_ask_passes_history_through(monkeypatch):
    captured = {}

    async def fake_answer_question(**kw):
        captured.update(kw)
        return {"answer": "ok", "sources": [], "truncated": False}

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    asyncio.run(
        service.ask(
            FakeNotebook(),
            "q?",
            config=FakeConfig(),
            embedder=object(),
            history=[{"role": "user", "content": "before"}],
        )
    )
    assert captured["history"] == [{"role": "user", "content": "before"}]


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


def test_generate_unknown_command_is_400():
    with pytest.raises(service.AskError) as exc:
        asyncio.run(
            service.generate(
                FakeNotebook(), "T", "translate", config=FakeConfig()
            )
        )
    assert exc.value.status == 400
    assert "summarize" in str(exc.value)


def test_generate_missing_tiddler_is_404():
    with pytest.raises(service.AskError) as exc:
        asyncio.run(
            service.generate(FakeNotebook(), "Nope", "summarize", config=FakeConfig())
        )
    assert exc.value.status == 404


def test_generate_empty_text_is_422():
    nbm = FakeNotebook(store={"T": {"title": "T", "text": "   "}})
    with pytest.raises(service.AskError) as exc:
        asyncio.run(service.generate(nbm, "T", "summarize", config=FakeConfig()))
    assert exc.value.status == 422


def test_generate_prefers_rendered_text(monkeypatch):
    nbm = FakeNotebook(
        store={"T": {"title": "T", "text": "{{transclusion}}"}},
        rendered={"T": "resolved text"},
    )
    seen = {}

    async def fake_run_command(command, title, text, cfg, vocabulary=None):
        seen["text"] = text
        return "sum"

    monkeypatch.setattr(service, "run_command", fake_run_command)
    result = asyncio.run(service.generate(nbm, "T", "summarize", config=FakeConfig()))
    assert seen["text"] == "resolved text"
    assert result == {"command": "summarize", "title": "T", "result": "sum"}


def test_generate_tags_returns_parsed_list(monkeypatch):
    nbm = FakeNotebook(store={"T": {"title": "T", "text": "body"}}, rendered={"T": "body"})

    async def fake_run_command(command, title, text, cfg, vocabulary=None):
        assert vocabulary == ["existing-tag", "another tag"]
        return "- homelab\n* another tag\n\n\"quoted\"\nfive\nsix\nseven"

    monkeypatch.setattr(service, "run_command", fake_run_command)
    result = asyncio.run(service.generate(nbm, "T", "tags", config=FakeConfig()))
    assert result["tags"] == ["homelab", "another tag", "quoted", "five", "six"]


def test_generate_translates_backend_failure(monkeypatch):
    nbm = FakeNotebook(store={"T": {"title": "T", "text": "body"}}, rendered={"T": "body"})

    async def fake_run_command(*a, **kw):
        raise GenerationError("The local LLM timed out")

    monkeypatch.setattr(service, "run_command", fake_run_command)
    with pytest.raises(service.AskError) as exc:
        asyncio.run(service.generate(nbm, "T", "summarize", config=FakeConfig()))
    assert exc.value.status == 503


# --- digest ---


def test_digest_skips_when_nothing_changed():
    nbm = FakeNotebook(tiddlers=[])
    result = asyncio.run(service.digest(nbm, config=FakeConfig()))
    assert result == {"written": False, "reason": "no recently modified notes"}
    assert nbm.seen_filters == [FakeConfig.digest_filter]
    assert nbm.written == []


def test_digest_writes_tagged_tiddler(monkeypatch):
    nbm = FakeNotebook(tiddlers=[{"title": "Changed", "text": "new stuff"}])

    async def fake_digest_text(tiddlers, cfg, period="the last 7 days"):
        assert [t["title"] for t in tiddlers] == ["Changed"]
        return "!! Digest\n* [[Changed]]", ["Changed"]

    monkeypatch.setattr(service, "ai_digest_text", fake_digest_text)
    result = asyncio.run(service.digest(nbm, config=FakeConfig()))
    assert result["written"] is True
    assert result["title"].startswith("AI Digest ")
    assert result["sources"] == ["Changed"]
    [(title, fields, text)] = nbm.written
    assert fields == {"tags": service.DIGEST_TAG}
    assert "[[Changed]]" in text


def test_digest_dry_run_does_not_write(monkeypatch):
    nbm = FakeNotebook(tiddlers=[{"title": "Changed", "text": "new stuff"}])

    async def fake_digest_text(tiddlers, cfg, period="the last 7 days"):
        return "digest", ["Changed"]

    monkeypatch.setattr(service, "ai_digest_text", fake_digest_text)
    result = asyncio.run(
        service.digest(nbm, config=FakeConfig(), write=False, title="Custom")
    )
    assert result["written"] is False
    assert result["title"] == "Custom"
    assert nbm.written == []


def test_digest_custom_filter_passes_through(monkeypatch):
    nbm = FakeNotebook(tiddlers=[])
    asyncio.run(service.digest(nbm, config=FakeConfig(), filter="[tag[x]]"))
    assert nbm.seen_filters == ["[tag[x]]"]
