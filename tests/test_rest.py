"""REST routes via TestClient with a stubbed AppManager — no Playwright, no
lifespan (module globals are injected directly)."""

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from app import main, service
from app.config import AppConfig
from app.embeddings import EmbeddingError
from app.note_cache import NoteCache, canonical_hash

API_KEY = "test-key"
AUTH = {"X-API-Key": API_KEY}


@dataclass
class FakeNotebook:
    store: dict = field(default_factory=dict)

    async def filter_tiddlers(self, filter, full=False):
        if full:
            return [
                {"title": t, "text": v.get("text", "")} for t, v in self.store.items()
            ]
        return list(self.store)

    async def get_tiddler(self, title):
        if title not in self.store:
            return None
        return {"title": title, **self.store[title]}

    async def put_tiddler(self, title, fields, text):
        self.store[title] = {"fields": fields, "text": text}
        return True

    async def delete_tiddler(self, title):
        self.store.pop(title, None)
        return True

    async def render(self, title, mode="plain"):
        return self.store.get(title, {}).get("text", "")


class FakeManager:
    def __init__(self, notebooks):
        self._notebooks = notebooks

    def notebook(self, name):
        return self._notebooks[name]


@pytest.fixture
def client(monkeypatch):
    nb = FakeNotebook(store={"Existing": {"text": "hello", "fields": {}}})
    cfg = AppConfig(
        api_key=API_KEY,
        gemini_api_key="gem-key",
        gemini_model="test-model",
        notebooks={"dev": None},  # only the keys are read outside the manager
    )
    monkeypatch.setattr(main, "_config", cfg)
    monkeypatch.setattr(main, "_manager", FakeManager({"dev": nb}))
    monkeypatch.setattr(main, "_embedder", object())
    monkeypatch.setattr(main, "_note_cache", NoteCache(":memory:"))
    # TestClient outside a `with` block does not run the lifespan.
    return TestClient(main.app, raise_server_exceptions=False), nb


def test_healthz_is_open(client):
    c, _ = client
    assert c.get("/healthz").status_code == 200


def test_missing_or_wrong_key_is_403(client):
    c, _ = client
    assert c.get("/notebooks").status_code == 403
    assert c.get("/notebooks", headers={"X-API-Key": "nope"}).status_code == 403


def test_mcp_mount_requires_key(client):
    c, _ = client
    assert c.get("/mcp/sse").status_code == 403
    assert c.get("/mcp/sse?key=nope").status_code == 403


def test_list_notebooks(client):
    c, _ = client
    resp = c.get("/notebooks", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"notebooks": ["dev"]}


def test_unknown_notebook_is_404(client):
    c, _ = client
    resp = c.get("/notebooks/nope/tiddlers", headers=AUTH)
    assert resp.status_code == 404


def test_get_tiddler_found_and_missing(client):
    c, _ = client
    ok = c.get("/notebooks/dev/tiddler", params={"title": "Existing"}, headers=AUTH)
    assert ok.status_code == 200
    assert ok.json()["text"] == "hello"
    missing = c.get("/notebooks/dev/tiddler", params={"title": "Nope"}, headers=AUTH)
    assert missing.status_code == 404


def test_put_tiddler_roundtrip(client):
    c, nb = client
    resp = c.put(
        "/notebooks/dev/tiddler",
        json={"title": "New", "text": "body", "fields": {"tags": "x"}},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert nb.store["New"] == {"fields": {"tags": "x"}, "text": "body"}


def test_ask_success(client, monkeypatch):
    c, _ = client

    async def fake_answer_question(**kw):
        return {"answer": "42", "sources": ["Existing"], "truncated": False}

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    resp = c.post("/notebooks/dev/ask", json={"question": "why?"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["answer"] == "42"


def test_ask_embedding_failure_is_friendly_503(client, monkeypatch):
    c, _ = client

    async def fake_answer_question(**kw):
        raise EmbeddingError("Ollama may still be starting up.")

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    resp = c.post("/notebooks/dev/ask", json={"question": "why?"}, headers=AUTH)
    assert resp.status_code == 503
    assert "Ollama" in resp.json()["detail"]


def test_related_success(client, monkeypatch):
    c, _ = client

    async def fake_related(target, tiddlers, embedder, top_k, max_embed=None):
        assert target["title"] == "Existing"
        return [{"title": "Neighbour", "score": 0.7}], False

    monkeypatch.setattr(service, "ai_related", fake_related)
    resp = c.get(
        "/notebooks/dev/related", params={"title": "Existing", "k": 3}, headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "related": [{"title": "Neighbour", "score": 0.7}],
        "truncated": False,
    }


def test_related_missing_title_is_404(client):
    c, _ = client
    resp = c.get("/notebooks/dev/related", params={"title": "Nope"}, headers=AUTH)
    assert resp.status_code == 404


def _sse_events(body: str) -> list[tuple[str, dict]]:
    import json

    events = []
    for frame in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_ask_stream_sse(client, monkeypatch):
    async def fake_stream(**kw):
        assert kw["history"] == [{"role": "user", "content": "before"}]
        yield ("delta", {"text": "Hi "})
        yield ("delta", {"text": "there"})
        yield ("done", {"answer": "Hi there", "sources": [], "truncated": False})

    monkeypatch.setattr(
        service, "answer_question_stream", lambda **kw: fake_stream(**kw)
    )
    c, _ = client
    resp = c.post(
        "/notebooks/dev/ask/stream",
        json={
            "question": "why?",
            "history": [{"role": "user", "content": "before"}],
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(resp.text)
    assert [name for name, _ in events] == ["delta", "delta", "done"]
    assert events[-1][1]["answer"] == "Hi there"


def test_ask_stream_bad_history_role_is_422(client):
    c, _ = client
    resp = c.post(
        "/notebooks/dev/ask/stream",
        json={"question": "q", "history": [{"role": "system", "content": "x"}]},
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_generate_route(client, monkeypatch):
    c, _ = client

    async def fake_run_command(command, title, text, cfg, vocabulary=None):
        assert (command, title, text) == ("summarize", "Existing", "hello")
        return "a summary"

    monkeypatch.setattr(service, "run_command", fake_run_command)
    resp = c.post(
        "/notebooks/dev/generate",
        json={"title": "Existing", "command": "summarize"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "command": "summarize",
        "title": "Existing",
        "result": "a summary",
    }


def test_generate_unknown_command_is_400(client):
    c, _ = client
    resp = c.post(
        "/notebooks/dev/generate",
        json={"title": "Existing", "command": "translate"},
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_digest_route_writes_tiddler(client, monkeypatch):
    c, nb = client

    async def fake_digest_text(tiddlers, cfg, period="the last 7 days"):
        return "!! What changed\n* [[Existing]]", ["Existing"]

    monkeypatch.setattr(service, "ai_digest_text", fake_digest_text)
    resp = c.post("/notebooks/dev/digest", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["written"] is True
    assert data["title"] in nb.store
    assert nb.store[data["title"]]["fields"] == {"tags": "ai-digest"}


# --- client-supplied-content routes (local mode) ---


ZEBRA = {"title": "Zebra", "text": "A zebra is a striped horse.", "fields": {}}
ZEBRA_HASH = canonical_hash("Zebra", "A zebra is a striped horse.", "")


def test_client_ask_works_without_any_notebook(client, monkeypatch):
    """The whole point of local mode: no manager, no configured notebook."""
    c, _ = client
    monkeypatch.setattr(main, "_manager", None)

    async def fake_answer_question(**kw):
        assert [t["title"] for t in kw["tiddlers"]] == ["Zebra"]
        return {"answer": "stripes", "sources": ["Zebra"], "truncated": False}

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    resp = c.post(
        "/ask", json={"question": "what is a zebra?", "tiddlers": [ZEBRA]}, headers=AUTH
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == "stripes"
    assert data["cache"] == {"hits": 0, "misses": 1}


def test_client_ask_cache_flow(client, monkeypatch):
    """Full send → check reports present → hash-only ref resolves from cache."""
    c, _ = client

    async def fake_answer_question(**kw):
        return {
            "answer": "ok",
            "sources": [t["title"] for t in kw["tiddlers"]],
            "truncated": False,
        }

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    first = c.post("/ask", json={"question": "q", "tiddlers": [ZEBRA]}, headers=AUTH)
    assert first.status_code == 200

    check = c.post("/notes/check", json={"hashes": [ZEBRA_HASH, "0" * 64]}, headers=AUTH)
    assert check.status_code == 200
    assert check.json() == {"missing": ["0" * 64]}

    second = c.post(
        "/ask",
        json={"question": "q", "tiddlers": [{"hash": ZEBRA_HASH}]},
        headers=AUTH,
    )
    assert second.status_code == 200
    data = second.json()
    assert data["sources"] == ["Zebra"]
    assert data["cache"] == {"hits": 1, "misses": 0}


def test_client_ask_unknown_ref_is_409_with_missing(client):
    c, _ = client
    resp = c.post(
        "/ask", json={"question": "q", "tiddlers": [{"hash": "f" * 64}]}, headers=AUTH
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"missing": ["f" * 64]}


def test_client_ask_stream_sse(client, monkeypatch):
    async def fake_stream(**kw):
        assert [t["title"] for t in kw["tiddlers"]] == ["Zebra"]
        yield ("delta", {"text": "Hi "})
        yield ("delta", {"text": "there"})
        yield ("done", {"answer": "Hi there", "sources": [], "truncated": False})

    monkeypatch.setattr(
        service, "answer_question_stream", lambda **kw: fake_stream(**kw)
    )
    c, _ = client
    resp = c.post(
        "/ask/stream",
        json={"question": "why?", "tiddlers": [ZEBRA]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(resp.text)
    assert [name for name, _ in events] == ["delta", "delta", "done"]
    assert events[-1][1]["answer"] == "Hi there"
    # done payload carries the cache stats the verify script asserts on.
    assert events[-1][1]["cache"] == {"hits": 0, "misses": 1}


def test_client_related(client, monkeypatch):
    async def fake_related(target, tiddlers, embedder, top_k, max_embed=None):
        assert target["title"] == "Zebra"
        assert [t["title"] for t in tiddlers] == ["Okapi"]
        return [{"title": "Okapi", "score": 0.8}], False

    monkeypatch.setattr(service, "ai_related", fake_related)
    c, _ = client
    resp = c.post(
        "/related",
        json={
            "target": ZEBRA,
            "tiddlers": [{"title": "Okapi", "text": "forest giraffe", "fields": {}}],
            "k": 3,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["related"] == [{"title": "Okapi", "score": 0.8}]
    assert data["cache"] == {"hits": 0, "misses": 1}


def test_client_related_ref_target_is_422(client):
    c, _ = client
    resp = c.post(
        "/related",
        json={"target": {"hash": "a" * 64}, "tiddlers": []},
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_client_ask_caps(client, monkeypatch):
    c, _ = client
    over_count = [
        {"title": f"T{i}", "text": "", "fields": {}}
        for i in range(main.MAX_CLIENT_TIDDLERS + 1)
    ]
    resp = c.post("/ask", json={"question": "q", "tiddlers": over_count}, headers=AUTH)
    assert resp.status_code == 422

    # Total-text budget: a few tiddlers under the per-tiddler cap but over 2M.
    big = [
        {"title": f"B{i}", "text": "x" * main.MAX_TIDDLER_TEXT, "fields": {}}
        for i in range(main.MAX_TOTAL_CHARS // main.MAX_TIDDLER_TEXT + 1)
    ]
    resp = c.post("/ask", json={"question": "q", "tiddlers": big}, headers=AUTH)
    assert resp.status_code == 422

    # Per-tiddler overflow is truncated, not rejected.
    captured = {}

    async def fake_answer_question(**kw):
        captured.update(kw)
        return {"answer": "ok", "sources": [], "truncated": False}

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    long_one = {"title": "Long", "text": "x" * (main.MAX_TIDDLER_TEXT + 1), "fields": {}}
    resp = c.post("/ask", json={"question": "q", "tiddlers": [long_one]}, headers=AUTH)
    assert resp.status_code == 200
    assert len(captured["tiddlers"][0]["text"]) == main.MAX_TIDDLER_TEXT


def test_client_tiddler_needs_title_or_hash(client):
    c, _ = client
    resp = c.post(
        "/ask", json={"question": "q", "tiddlers": [{"text": "orphan"}]}, headers=AUTH
    )
    assert resp.status_code == 422


def test_client_routes_require_auth(client):
    c, _ = client
    assert c.post("/ask", json={"question": "q"}).status_code == 403
    assert c.post("/ask/stream", json={"question": "q"}).status_code == 403
    assert c.post("/related", json={"target": ZEBRA}).status_code == 403
    assert c.post("/notes/check", json={"hashes": []}).status_code == 403


def test_client_ask_backend_failure_is_503(client, monkeypatch):
    c, _ = client

    async def fake_answer_question(**kw):
        raise EmbeddingError("Ollama may still be starting up.")

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    resp = c.post("/ask", json={"question": "q", "tiddlers": [ZEBRA]}, headers=AUTH)
    assert resp.status_code == 503
    assert "Ollama" in resp.json()["detail"]


def test_ask_unexpected_error_is_500_with_cors(client, monkeypatch):
    """Unhandled exceptions must become HTTPExceptions so the response still
    flows through CORSMiddleware (a bare 500 looks like a CORS error in the
    browser plugin)."""
    c, _ = client

    async def fake_answer_question(**kw):
        raise ValueError("surprise")

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    resp = c.post(
        "/notebooks/dev/ask",
        json={"question": "why?"},
        headers={**AUTH, "Origin": "https://tw-dev.lab.cc"},
    )
    assert resp.status_code == 500
    assert resp.json()["detail"] == "surprise"
    assert resp.headers.get("access-control-allow-origin") == "*"
