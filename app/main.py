import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator, model_validator

from . import service
from .config import AppConfig, load_config
from .embeddings import Embedder
from .note_cache import NoteCache
from .prewarm import EmbedPrewarmer

logger = logging.getLogger(__name__)

_config: AppConfig | None = None
_embedder: Embedder | None = None
_note_cache: NoteCache | None = None
_prewarmer: EmbedPrewarmer | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _embedder, _note_cache, _prewarmer
    # Uvicorn only configures its own loggers; give the app's INFO logs
    # (embedder cache stats, prewarm progress) a handler too.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: [%(name)s] %(message)s")
    _config = load_config()
    # Cache lives on the profiles named volume so restarts don't re-embed.
    _embedder = Embedder(
        _config.ollama_url,
        _config.embed_model,
        cache_path=os.path.join(_config.profiles_dir, "embeddings.sqlite3"),
    )
    # Note cache for the plugin's hash refs — same volume, same warm-restart story.
    _note_cache = NoteCache(os.path.join(_config.profiles_dir, "note_cache.sqlite3"))
    _note_cache.prune()
    # Embeds ingested notes off the request path so asks hit a warm vector cache.
    _prewarmer = EmbedPrewarmer(_embedder)
    _prewarmer.start()
    yield
    await _prewarmer.aclose()
    _note_cache.close()
    await _embedder.aclose()


app = FastAPI(title="Familiar", lifespan=lifespan)

# Permissive CORS for LAN/plugin use. Origin validation is left to the API
# key — restricting origins here would only help if the key were also secret
# from the browser, which it isn't once embedded in a plugin config tiddler.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

def _key_matches(candidate: str | None) -> bool:
    if not _config or not candidate:
        return False
    # Compare bytes: compare_digest raises TypeError on non-ASCII str, which
    # would turn a bad key into a 500 instead of a 403.
    return secrets.compare_digest(
        candidate.encode("utf-8"), _config.api_key.encode("utf-8")
    )


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_auth(x_api_key: str = Depends(_api_key_header)):
    if not _key_matches(x_api_key):
        raise HTTPException(status_code=403, detail="Invalid or missing API key")


def _sse_response(events) -> StreamingResponse:
    async def sse():
        async for name, data in events:
            yield f"event: {name}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- request models ---
#
# All note content arrives in request bodies from the plugin (full tiddlers or
# hash refs into the note cache). The server holds no wiki credentials and
# runs no browser.

# Server-side budgets for content sent in request bodies. The plugin enforces
# the same limits (dropping overflow), so the 422s here are backstops for
# misbehaving clients, not paths a healthy plugin ever hits.
MAX_CLIENT_TIDDLERS = 500
MAX_TIDDLER_TEXT = 50_000
MAX_TOTAL_CHARS = 2_000_000


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ClientTiddler(BaseModel):
    """One note in a client payload: full content ({title, text, fields}) or a
    bare {hash} reference into the note cache. A `title` makes it full (a
    client-supplied hash alongside is ignored — the server recomputes)."""

    hash: Optional[str] = None
    title: Optional[str] = None
    text: str = ""
    fields: dict = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def _truncate(cls, v: str) -> str:
        return v[:MAX_TIDDLER_TEXT]

    @model_validator(mode="after")
    def _full_or_ref(self):
        if self.title is not None and not self.title:
            raise ValueError("title must be non-empty")
        if self.title is None and not self.hash:
            raise ValueError("tiddler needs either 'title' (full) or 'hash' (ref)")
        return self


def _check_total_budget(tiddlers: list[ClientTiddler]) -> None:
    total = sum(len(t.text) for t in tiddlers if t.title is not None)
    if total > MAX_TOTAL_CHARS:
        raise ValueError(f"total tiddler text exceeds {MAX_TOTAL_CHARS} chars")


class ClientAskBody(BaseModel):
    question: str
    tiddlers: list[ClientTiddler] = Field(
        default_factory=list, max_length=MAX_CLIENT_TIDDLERS
    )
    # Bounds embedding-cache *misses* per request (cost control), not the
    # candidate pool — every sent note participates in ranking once cached.
    max_tiddlers: int = Field(100, ge=1, le=1000)
    # Prior chat turns, oldest first. Trimmed server-side to a turn/char budget.
    history: list[ChatTurn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _budget(self):
        _check_total_budget(self.tiddlers)
        return self


class ClientRelatedBody(BaseModel):
    target: ClientTiddler
    tiddlers: list[ClientTiddler] = Field(
        default_factory=list, max_length=MAX_CLIENT_TIDDLERS
    )
    k: int = Field(5, ge=1, le=50)
    max_tiddlers: int = Field(100, ge=1, le=1000)

    @model_validator(mode="after")
    def _budget(self):
        if self.target.title is None:
            raise ValueError("target must be a full tiddler, not a hash ref")
        _check_total_budget(self.tiddlers)
        return self


class ClientSearchBody(BaseModel):
    query: str = Field(min_length=1)
    tiddlers: list[ClientTiddler] = Field(
        default_factory=list, max_length=MAX_CLIENT_TIDDLERS
    )
    k: int = Field(10, ge=1, le=50)
    max_tiddlers: int = Field(100, ge=1, le=1000)

    @model_validator(mode="after")
    def _budget(self):
        _check_total_budget(self.tiddlers)
        return self


class ClientGenerateBody(BaseModel):
    """Generate over client-supplied text: the browser renders the note to
    plain text itself (the server can't render a wiki it has no session for)
    and, for the tags command, sends its own tag vocabulary."""

    title: str = Field(min_length=1)
    text: str = ""
    command: str
    vocabulary: list[str] = Field(default_factory=list, max_length=2000)

    @field_validator("text")
    @classmethod
    def _truncate(cls, v: str) -> str:
        return v[:MAX_TIDDLER_TEXT]


class NotesCheckBody(BaseModel):
    hashes: list[str] = Field(default_factory=list, max_length=2000)


class NotesSyncBody(BaseModel):
    """Background upload from the plugin: full tiddlers only (a hash ref has
    nothing to store). Same budgets as the ask routes."""

    tiddlers: list[ClientTiddler] = Field(
        default_factory=list, max_length=MAX_CLIENT_TIDDLERS
    )

    @model_validator(mode="after")
    def _fulls_within_budget(self):
        if any(t.title is None for t in self.tiddlers):
            raise ValueError("sync accepts full tiddlers only, not hash refs")
        _check_total_budget(self.tiddlers)
        return self


def _resolve_client_tiddlers(items: list[ClientTiddler]) -> tuple[list[dict], dict]:
    """Turn a mixed full/ref list into plain tiddler dicts (original order).

    Full tiddlers are stored into the note cache; refs are resolved from it.
    Unknown refs → 409 with the missing hashes so the client can resend them
    in full (eviction between /notes/check and the ask is a benign race).
    Returns (tiddlers, {"hits": refs_resolved, "misses": fulls_received}).
    """
    refs = [t.hash for t in items if t.title is None]
    resolved = _note_cache.get_many(refs) if refs else {}
    missing = [h for h in refs if h not in resolved]
    if missing:
        raise HTTPException(status_code=409, detail={"missing": missing})
    fulls = [
        {"title": t.title, "text": t.text, "fields": t.fields}
        for t in items
        if t.title is not None
    ]
    if fulls:
        _note_cache.put_many(fulls)
        # Warm their embeddings in the background so the *next* ask over this
        # content skips inline embedding even if this one can't.
        if _prewarmer is not None:
            _prewarmer.enqueue(fulls)
    out = [
        {"title": t.title, "text": t.text, "fields": t.fields}
        if t.title is not None
        else resolved[t.hash]
        for t in items
    ]
    return out, {"hits": len(refs), "misses": len(fulls)}


# --- routes ---


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/notes/check", dependencies=[Depends(require_auth)])
async def notes_check(body: NotesCheckBody):
    """Pre-flight for ask/related requests: which of these note hashes must be
    sent in full? Known hashes get their retention window bumped."""
    present = _note_cache.check(body.hashes)
    return {"missing": [h for h in body.hashes if h not in present]}


@app.post("/notes/sync", dependencies=[Depends(require_auth)])
async def notes_sync(body: NotesSyncBody):
    """Background sync from the plugin: store full tiddlers now, embed them
    off the request path, so later asks are pure hash refs over a warm cache."""
    tiddlers = [
        {"title": t.title, "text": t.text, "fields": t.fields} for t in body.tiddlers
    ]
    if tiddlers:
        _note_cache.put_many(tiddlers)
        if _prewarmer is not None:
            _prewarmer.enqueue(tiddlers)
    return {"stored": len(tiddlers)}


@app.post("/ask", dependencies=[Depends(require_auth)])
async def client_ask(body: ClientAskBody):
    tiddlers, cache_stats = _resolve_client_tiddlers(body.tiddlers)
    try:
        result = await service.ask_with_tiddlers(
            body.question,
            tiddlers,
            config=_config,
            embedder=_embedder,
            max_tiddlers=body.max_tiddlers,
            history=[t.model_dump() for t in body.history],
        )
    except service.AskError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception as e:
        # Re-raise as HTTPException so it flows through CORSMiddleware —
        # unhandled exceptions bypass it and the browser sees a CORS error.
        raise HTTPException(status_code=500, detail=str(e))
    return {**result, "cache": cache_stats}


@app.post("/ask/stream", dependencies=[Depends(require_auth)])
async def client_ask_stream(body: ClientAskBody):
    """SSE variant of /ask: `delta` events carry answer fragments, one final
    `done` event carries {answer, sources, truncated, cache}. Backend failures
    after the stream has started arrive as an `error` event (the 200 is
    already on the wire by then); failures before that — including unknown
    hash refs (409) — are normal HTTP errors."""
    tiddlers, cache_stats = _resolve_client_tiddlers(body.tiddlers)
    try:
        events = await service.ask_stream_with_tiddlers(
            body.question,
            tiddlers,
            config=_config,
            embedder=_embedder,
            max_tiddlers=body.max_tiddlers,
            history=[t.model_dump() for t in body.history],
        )
    except service.AskError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    async def with_cache_stats():
        async for name, data in events:
            if name == "done":
                data = {**data, "cache": cache_stats}
            yield name, data

    return _sse_response(with_cache_stats())


@app.post("/generate", dependencies=[Depends(require_auth)])
async def client_generate(body: ClientGenerateBody):
    """One-shot generation command (summarize / tags / title / tasks) over
    text the client rendered itself. No note-cache involvement: the payload
    is a single rendered note, not raw wiki content."""
    try:
        return await service.generate_with_text(
            body.title,
            body.text,
            body.command,
            config=_config,
            vocabulary=body.vocabulary or None,
        )
    except service.AskError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/search", dependencies=[Depends(require_auth)])
async def client_search(body: ClientSearchBody):
    """Ranked semantic search over the sent notes — same hybrid retrieval as
    /ask, but returns scored {title, snippet} results with no generation."""
    tiddlers, cache_stats = _resolve_client_tiddlers(body.tiddlers)
    try:
        result = await service.search_with_tiddlers(
            body.query,
            tiddlers,
            embedder=_embedder,
            k=body.k,
            max_tiddlers=body.max_tiddlers,
        )
    except service.AskError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {**result, "cache": cache_stats}


@app.post("/related", dependencies=[Depends(require_auth)])
async def client_related(body: ClientRelatedBody):
    tiddlers, cache_stats = _resolve_client_tiddlers(body.tiddlers)
    target = {
        "title": body.target.title,
        "text": body.target.text,
        "fields": body.target.fields,
    }
    try:
        result = await service.related_with_tiddlers(
            target,
            tiddlers,
            embedder=_embedder,
            k=body.k,
            max_tiddlers=body.max_tiddlers,
        )
    except service.AskError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {**result, "cache": cache_stats}
