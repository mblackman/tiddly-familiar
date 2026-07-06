"""REST routes via TestClient — no lifespan (module globals are injected
directly). All note content arrives in request bodies; there is no notebook
backend to stub."""

import pytest
from fastapi.testclient import TestClient

from app import main, service
from app.config import AppConfig
from app.embeddings import EmbeddingError
from app.note_cache import NoteCache, canonical_hash

API_KEY = "test-key"
AUTH = {"X-API-Key": API_KEY}


class RecordingPrewarmer:
    """Stands in for main._prewarmer: records what routes enqueue."""

    def __init__(self):
        self.enqueued: list[dict] = []

    def enqueue(self, tiddlers):
        self.enqueued.extend(tiddlers)


@pytest.fixture
def client(monkeypatch):
    cfg = AppConfig(
        api_key=API_KEY,
        gemini_api_key="gem-key",
        gemini_model="test-model",
    )
    monkeypatch.setattr(main, "_config", cfg)
    monkeypatch.setattr(main, "_embedder", object())
    monkeypatch.setattr(main, "_note_cache", NoteCache(":memory:"))
    monkeypatch.setattr(main, "_prewarmer", RecordingPrewarmer())
    # TestClient outside a `with` block does not run the lifespan.
    return TestClient(main.app, raise_server_exceptions=False)


def test_healthz_is_open(client):
    assert client.get("/healthz").status_code == 200


def test_missing_or_wrong_key_is_403(client):
    assert client.post("/ask", json={"question": "q"}).status_code == 403
    resp = client.post(
        "/ask", json={"question": "q"}, headers={"X-API-Key": "nope"}
    )
    assert resp.status_code == 403


ZEBRA = {"title": "Zebra", "text": "A zebra is a striped horse.", "fields": {}}
ZEBRA_HASH = canonical_hash("Zebra", "A zebra is a striped horse.", "")


def test_client_ask(client, monkeypatch):
    async def fake_answer_question(**kw):
        assert [t["title"] for t in kw["tiddlers"]] == ["Zebra"]
        return {"answer": "stripes", "sources": ["Zebra"], "truncated": False}

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    resp = client.post(
        "/ask", json={"question": "what is a zebra?", "tiddlers": [ZEBRA]}, headers=AUTH
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == "stripes"
    assert data["cache"] == {"hits": 0, "misses": 1}


def test_client_ask_overrides_config_per_request(client, monkeypatch):
    """rag_top_k / query_rewrite in the body override the server config for that
    request only; the shared _config is left untouched."""
    seen = {}

    async def fake_answer_question(**kw):
        cfg = kw["cfg"]
        seen["rag_top_k"] = cfg.rag_top_k
        seen["query_rewrite"] = cfg.query_rewrite
        return {"answer": "ok", "sources": [], "truncated": False}

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    resp = client.post(
        "/ask",
        json={
            "question": "q",
            "tiddlers": [ZEBRA],
            "rag_top_k": 3,
            "query_rewrite": False,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert seen == {"rag_top_k": 3, "query_rewrite": False}
    # the module-global config keeps its defaults for the next request
    assert main._config.rag_top_k == 8
    assert main._config.query_rewrite is True


def test_client_ask_without_overrides_uses_server_config(client, monkeypatch):
    seen = {}

    async def fake_answer_question(**kw):
        seen["cfg"] = kw["cfg"]
        return {"answer": "ok", "sources": [], "truncated": False}

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    resp = client.post("/ask", json={"question": "q", "tiddlers": [ZEBRA]}, headers=AUTH)
    assert resp.status_code == 200
    # no overrides → the very same config object, not a copy
    assert seen["cfg"] is main._config


def test_client_ask_override_bounds_are_validated(client):
    for bad in (0, 51):
        resp = client.post(
            "/ask",
            json={"question": "q", "tiddlers": [ZEBRA], "rag_top_k": bad},
            headers=AUTH,
        )
        assert resp.status_code == 422


def test_client_ask_cache_flow(client, monkeypatch):
    """Full send → check reports present → hash-only ref resolves from cache."""

    async def fake_answer_question(**kw):
        return {
            "answer": "ok",
            "sources": [t["title"] for t in kw["tiddlers"]],
            "truncated": False,
        }

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    first = client.post("/ask", json={"question": "q", "tiddlers": [ZEBRA]}, headers=AUTH)
    assert first.status_code == 200

    check = client.post(
        "/notes/check", json={"hashes": [ZEBRA_HASH, "0" * 64]}, headers=AUTH
    )
    assert check.status_code == 200
    assert check.json() == {"missing": ["0" * 64]}

    second = client.post(
        "/ask",
        json={"question": "q", "tiddlers": [{"hash": ZEBRA_HASH}]},
        headers=AUTH,
    )
    assert second.status_code == 200
    data = second.json()
    assert data["sources"] == ["Zebra"]
    assert data["cache"] == {"hits": 1, "misses": 0}


def test_notes_sync_stores_for_later_hash_refs(client, monkeypatch):
    """Background sync warms the note cache: a later ask can be pure refs."""

    async def fake_answer_question(**kw):
        return {
            "answer": "ok",
            "sources": [t["title"] for t in kw["tiddlers"]],
            "truncated": False,
        }

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    resp = client.post("/notes/sync", json={"tiddlers": [ZEBRA]}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"stored": 1}

    ask = client.post(
        "/ask",
        json={"question": "q", "tiddlers": [{"hash": ZEBRA_HASH}]},
        headers=AUTH,
    )
    assert ask.status_code == 200
    assert ask.json()["cache"] == {"hits": 1, "misses": 0}


def test_notes_sync_rejects_hash_refs(client):
    resp = client.post(
        "/notes/sync", json={"tiddlers": [{"hash": "f" * 64}]}, headers=AUTH
    )
    assert resp.status_code == 422


def test_ingest_schedules_embedding_prewarm(client, monkeypatch):
    """Both ingest points — sync and full sends on ask — enqueue for the
    background embed worker; refs (already-cached content) do not."""

    async def fake_answer_question(**kw):
        return {"answer": "ok", "sources": [], "truncated": False}

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    client.post("/notes/sync", json={"tiddlers": [ZEBRA]}, headers=AUTH)
    assert [t["title"] for t in main._prewarmer.enqueued] == ["Zebra"]

    other = {"title": "Okapi", "text": "A forest giraffe.", "fields": {}}
    client.post(
        "/ask",
        json={"question": "q", "tiddlers": [{"hash": ZEBRA_HASH}, other]},
        headers=AUTH,
    )
    assert [t["title"] for t in main._prewarmer.enqueued] == ["Zebra", "Okapi"]


def test_client_ask_unknown_ref_is_409_with_missing(client):
    resp = client.post(
        "/ask", json={"question": "q", "tiddlers": [{"hash": "f" * 64}]}, headers=AUTH
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"missing": ["f" * 64]}


def _sse_events(body: str) -> list[tuple[str, dict]]:
    import json

    events = []
    for frame in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_client_ask_stream_sse(client, monkeypatch):
    async def fake_stream(**kw):
        assert [t["title"] for t in kw["tiddlers"]] == ["Zebra"]
        assert kw["history"] == [{"role": "user", "content": "before"}]
        yield ("delta", {"text": "Hi "})
        yield ("delta", {"text": "there"})
        yield ("done", {"answer": "Hi there", "sources": [], "truncated": False})

    monkeypatch.setattr(
        service, "answer_question_stream", lambda **kw: fake_stream(**kw)
    )
    resp = client.post(
        "/ask/stream",
        json={
            "question": "why?",
            "tiddlers": [ZEBRA],
            "history": [{"role": "user", "content": "before"}],
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(resp.text)
    assert [name for name, _ in events] == ["delta", "delta", "done"]
    assert events[-1][1]["answer"] == "Hi there"
    # done payload carries the cache stats the verify script asserts on.
    assert events[-1][1]["cache"] == {"hits": 0, "misses": 1}


def test_ask_stream_bad_history_role_is_422(client):
    resp = client.post(
        "/ask/stream",
        json={"question": "q", "history": [{"role": "system", "content": "x"}]},
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_client_related(client, monkeypatch):
    async def fake_related(target, tiddlers, embedder, top_k, max_embed=None):
        assert target["title"] == "Zebra"
        assert [t["title"] for t in tiddlers] == ["Okapi"]
        return [{"title": "Okapi", "score": 0.8}], False

    monkeypatch.setattr(service, "ai_related", fake_related)
    resp = client.post(
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


def test_client_search(client, monkeypatch):
    async def fake_search(query, tiddlers, embedder, top_k, max_embed=None):
        assert query == "striped horse"
        assert [t["title"] for t in tiddlers] == ["Zebra"]
        return [{"title": "Zebra", "score": 0.91, "snippet": "striped horse."}], False

    monkeypatch.setattr(service, "ai_search", fake_search)
    resp = client.post(
        "/search",
        json={"query": "striped horse", "tiddlers": [ZEBRA], "k": 5},
        headers=AUTH,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == [
        {"title": "Zebra", "score": 0.91, "snippet": "striped horse."}
    ]
    assert data["cache"] == {"hits": 0, "misses": 1}


def test_client_search_hash_ref_resolves_from_cache(client, monkeypatch):
    """Search shares the note cache: a warmed hash ref needs no full send."""

    async def fake_search(query, tiddlers, embedder, top_k, max_embed=None):
        return [{"title": t["title"], "score": 1.0, "snippet": ""} for t in tiddlers], False

    monkeypatch.setattr(service, "ai_search", fake_search)
    client.post("/notes/sync", json={"tiddlers": [ZEBRA]}, headers=AUTH)
    resp = client.post(
        "/search",
        json={"query": "q", "tiddlers": [{"hash": ZEBRA_HASH}]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["cache"] == {"hits": 1, "misses": 0}


def test_client_generate(client, monkeypatch):
    async def fake_run_command(command, title, text, cfg, vocabulary=None):
        assert (command, title, text) == ("summarize", "Zebra", "rendered text")
        assert vocabulary is None
        return "a summary"

    monkeypatch.setattr(service, "run_command", fake_run_command)
    resp = client.post(
        "/generate",
        json={"title": "Zebra", "text": "rendered text", "command": "summarize"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json() == {"command": "summarize", "title": "Zebra", "result": "a summary"}


def test_client_generate_guards(client):
    bad_cmd = client.post(
        "/generate",
        json={"title": "T", "text": "x", "command": "translate"},
        headers=AUTH,
    )
    assert bad_cmd.status_code == 400
    empty = client.post(
        "/generate",
        json={"title": "T", "text": "  ", "command": "summarize"},
        headers=AUTH,
    )
    assert empty.status_code == 422
    assert client.post("/generate", json={"title": "T", "command": "summarize"}).status_code == 403


def test_client_related_ref_target_is_422(client):
    resp = client.post(
        "/related",
        json={"target": {"hash": "a" * 64}, "tiddlers": []},
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_client_ask_caps(client, monkeypatch):
    over_count = [
        {"title": f"T{i}", "text": "", "fields": {}}
        for i in range(main.MAX_CLIENT_TIDDLERS + 1)
    ]
    resp = client.post("/ask", json={"question": "q", "tiddlers": over_count}, headers=AUTH)
    assert resp.status_code == 422

    # Total-text budget: a few tiddlers under the per-tiddler cap but over 2M.
    big = [
        {"title": f"B{i}", "text": "x" * main.MAX_TIDDLER_TEXT, "fields": {}}
        for i in range(main.MAX_TOTAL_CHARS // main.MAX_TIDDLER_TEXT + 1)
    ]
    resp = client.post("/ask", json={"question": "q", "tiddlers": big}, headers=AUTH)
    assert resp.status_code == 422

    # Per-tiddler overflow is truncated, not rejected.
    captured = {}

    async def fake_answer_question(**kw):
        captured.update(kw)
        return {"answer": "ok", "sources": [], "truncated": False}

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    long_one = {"title": "Long", "text": "x" * (main.MAX_TIDDLER_TEXT + 1), "fields": {}}
    resp = client.post("/ask", json={"question": "q", "tiddlers": [long_one]}, headers=AUTH)
    assert resp.status_code == 200
    assert len(captured["tiddlers"][0]["text"]) == main.MAX_TIDDLER_TEXT


def test_client_tiddler_needs_title_or_hash(client):
    resp = client.post(
        "/ask", json={"question": "q", "tiddlers": [{"text": "orphan"}]}, headers=AUTH
    )
    assert resp.status_code == 422


def test_client_routes_require_auth(client):
    assert client.post("/ask", json={"question": "q"}).status_code == 403
    assert client.post("/ask/stream", json={"question": "q"}).status_code == 403
    assert client.post("/related", json={"target": ZEBRA}).status_code == 403
    assert client.post("/search", json={"query": "q"}).status_code == 403
    assert client.post("/notes/check", json={"hashes": []}).status_code == 403
    assert client.post("/notes/sync", json={"tiddlers": []}).status_code == 403


def test_client_ask_backend_failure_is_503(client, monkeypatch):
    async def fake_answer_question(**kw):
        raise EmbeddingError("Ollama may still be starting up.")

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    resp = client.post("/ask", json={"question": "q", "tiddlers": [ZEBRA]}, headers=AUTH)
    assert resp.status_code == 503
    assert "Ollama" in resp.json()["detail"]


def test_ask_unexpected_error_is_500_with_cors(client, monkeypatch):
    """Unhandled exceptions must become HTTPExceptions so the response still
    flows through CORSMiddleware (a bare 500 looks like a CORS error in the
    browser plugin)."""

    async def fake_answer_question(**kw):
        raise ValueError("surprise")

    monkeypatch.setattr(service, "answer_question", fake_answer_question)
    resp = client.post(
        "/ask",
        json={"question": "why?", "tiddlers": [ZEBRA]},
        headers={**AUTH, "Origin": "https://tw-dev.lab.cc"},
    )
    assert resp.status_code == 500
    assert resp.json()["detail"] == "surprise"
    assert resp.headers.get("access-control-allow-origin") == "*"
