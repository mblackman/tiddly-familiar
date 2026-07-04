import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator, model_validator

from . import service
from .config import AppConfig, load_config
from .embeddings import Embedder
from .manager import AppManager
from .mcp_server import init as mcp_init
from .mcp_server import mcp
from .note_cache import NoteCache

logger = logging.getLogger(__name__)

_config: AppConfig | None = None
_manager: AppManager | None = None
_embedder: Embedder | None = None
_note_cache: NoteCache | None = None


async def _digest_scheduler():
    """Daily synthesis digest: sleep until the configured UTC hour, write a
    what-changed tiddler into the digest notebook, repeat. Failures are logged
    and retried at the next tick — a flaky backend must not kill the loop."""
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(
            hour=_config.digest_hour_utc, minute=0, second=0, microsecond=0
        )
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            nbm = _manager.notebook(_config.digest_notebook)
            result = await service.digest(nbm, config=_config)
            if result["written"]:
                logger.info("Scheduled digest written: %s", result["title"])
            else:
                logger.info("Scheduled digest skipped: %s", result["reason"])
        except Exception:
            logger.exception("Scheduled digest failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _manager, _embedder, _note_cache
    _config = load_config()
    _manager = AppManager()
    await _manager.start(_config.notebooks, _config.profiles_dir)
    # Cache lives on the profiles named volume so restarts don't re-embed.
    _embedder = Embedder(
        _config.ollama_url,
        _config.embed_model,
        cache_path=os.path.join(_config.profiles_dir, "embeddings.sqlite3"),
    )
    # Note cache for local-mode clients — same volume, same warm-restart story.
    _note_cache = NoteCache(os.path.join(_config.profiles_dir, "note_cache.sqlite3"))
    _note_cache.prune()
    mcp_init(_config, _manager, _embedder)
    digest_task = None
    if _config.digest_notebook:
        if _config.digest_notebook in _config.notebooks:
            logger.info(
                "Digest scheduler on: notebook '%s' daily at %02d:00 UTC",
                _config.digest_notebook,
                _config.digest_hour_utc,
            )
            digest_task = asyncio.create_task(_digest_scheduler())
        else:
            logger.error(
                "DIGEST_NOTEBOOK '%s' is not a configured notebook — scheduler off",
                _config.digest_notebook,
            )
    yield
    if digest_task:
        digest_task.cancel()
    _note_cache.close()
    await _embedder.aclose()
    await _manager.stop()


app = FastAPI(title="TiddlyPWA Gateway", lifespan=lifespan)

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


class MCPAuthMiddleware:
    """Require the gateway API key on every request into the MCP mount.

    The SSE app bypasses FastAPI dependencies, so auth happens here at the ASGI
    layer. Accepts the X-API-Key header or a ?key= query parameter (some MCP
    clients can't set custom headers)."""

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            key = None
            for name, value in scope.get("headers", []):
                if name == b"x-api-key":
                    key = value.decode("latin-1")
                    break
            if not key:
                qs = parse_qs(scope.get("query_string", b"").decode())
                key = (qs.get("key") or [None])[0]
            if not _key_matches(key):
                response = JSONResponse(
                    {"detail": "Invalid or missing API key"}, status_code=403
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


# Mount the MCP server at /mcp (SSE transport for Claude desktop/web/mobile).
# MCP tools include write/delete, so the mount is gated on the same API key as
# the REST routes — required before the gateway is exposed on the LAN via Caddy.
app.mount("/mcp", MCPAuthMiddleware(mcp.sse_app()))

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_auth(x_api_key: str = Depends(_api_key_header)):
    if not _key_matches(x_api_key):
        raise HTTPException(status_code=403, detail="Invalid or missing API key")


def _get_notebook(nb: str):
    try:
        return _manager.notebook(nb)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Notebook '{nb}' not found")


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


class TiddlerBody(BaseModel):
    title: str
    fields: dict = Field(default_factory=dict)
    text: str = ""


class RenderBody(BaseModel):
    title: Optional[str] = None
    text: Optional[str] = None
    as_: str = Field("plain", alias="as")
    model_config = {"populate_by_name": True}


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class AskBody(BaseModel):
    question: str
    filter: Optional[str] = None
    # Bounds embedding-cache *misses* per request (cost control), not the
    # candidate pool — every filter match participates in ranking once cached.
    max_tiddlers: int = Field(100, ge=1, le=1000)
    # Prior chat turns, oldest first. Trimmed server-side to a turn/char budget.
    history: list[ChatTurn] = Field(default_factory=list)


class GenerateBody(BaseModel):
    title: str
    command: str


class DigestBody(BaseModel):
    filter: Optional[str] = None
    title: Optional[str] = None
    write: bool = True


# --- client-supplied-content models (local mode) ---

# Server-side budgets for content sent in request bodies. The plugin enforces
# the same limits (dropping overflow), so the 422s here are backstops for
# misbehaving clients, not paths a healthy plugin ever hits.
MAX_CLIENT_TIDDLERS = 500
MAX_TIDDLER_TEXT = 50_000
MAX_TOTAL_CHARS = 2_000_000


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
    max_tiddlers: int = Field(100, ge=1, le=1000)
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


class NotesCheckBody(BaseModel):
    hashes: list[str] = Field(default_factory=list, max_length=2000)


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


@app.get("/notebooks", dependencies=[Depends(require_auth)])
async def list_notebooks():
    return {"notebooks": list(_config.notebooks.keys())}


@app.get("/notebooks/{nb}/probe", dependencies=[Depends(require_auth)])
async def probe_notebook(nb: str):
    nbm = _get_notebook(nb)
    return await nbm.probe()


@app.get("/notebooks/{nb}/tiddlers", dependencies=[Depends(require_auth)])
async def list_tiddlers(
    nb: str,
    filter: str = Query("[!is[system]]"),
    full: bool = False,
):
    nbm = _get_notebook(nb)
    return await nbm.filter_tiddlers(filter, full=full)


@app.get("/notebooks/{nb}/tiddler", dependencies=[Depends(require_auth)])
async def get_tiddler(nb: str, title: str):
    nbm = _get_notebook(nb)
    result = await nbm.get_tiddler(title)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Tiddler '{title}' not found")
    return result


@app.put("/notebooks/{nb}/tiddler", dependencies=[Depends(require_auth)])
async def put_tiddler(nb: str, body: TiddlerBody):
    nbm = _get_notebook(nb)
    await nbm.put_tiddler(body.title, body.fields, body.text)
    return {"ok": True}


@app.delete("/notebooks/{nb}/tiddler", dependencies=[Depends(require_auth)])
async def delete_tiddler(nb: str, title: str):
    nbm = _get_notebook(nb)
    await nbm.delete_tiddler(title)
    return {"ok": True}


@app.post("/notebooks/{nb}/render", dependencies=[Depends(require_auth)])
async def render(nb: str, body: RenderBody):
    if not body.title and body.text is None:
        raise HTTPException(status_code=422, detail="Provide 'title' or 'text'")
    nbm = _get_notebook(nb)
    if body.title:
        content = await nbm.render(body.title, mode=body.as_)
    else:
        content = await nbm.render_text(body.text, mode=body.as_)
    return {"content": content}


@app.post("/notebooks/{nb}/sync", dependencies=[Depends(require_auth)])
async def sync(nb: str):
    nbm = _get_notebook(nb)
    await nbm.sync()
    return {"ok": True}


@app.get("/notebooks/{nb}/related", dependencies=[Depends(require_auth)])
async def related(
    nb: str,
    title: str,
    k: int = Query(5, ge=1, le=50),
    filter: str = Query("[!is[system]]"),
    max_tiddlers: int = Query(100, ge=1, le=1000),
):
    nbm = _get_notebook(nb)
    try:
        return await service.related(
            nbm,
            title,
            config=_config,
            embedder=_embedder,
            k=k,
            filter=filter,
            max_tiddlers=max_tiddlers,
        )
    except service.AskError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception as e:
        # Same CORS rationale as /ask below.
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/notebooks/{nb}/ask", dependencies=[Depends(require_auth)])
async def ask(nb: str, body: AskBody):
    nbm = _get_notebook(nb)
    try:
        return await service.ask(
            nbm,
            body.question,
            config=_config,
            embedder=_embedder,
            filter=body.filter,
            max_tiddlers=body.max_tiddlers,
            history=[t.model_dump() for t in body.history],
        )
    except service.AskError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception as e:
        # Re-raise as HTTPException so it flows through CORSMiddleware —
        # unhandled exceptions bypass it and the browser sees a CORS error.
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/notebooks/{nb}/ask/stream", dependencies=[Depends(require_auth)])
async def ask_stream(nb: str, body: AskBody):
    """SSE variant of /ask: `delta` events carry answer fragments, one final
    `done` event carries {answer, sources, truncated}. Backend failures after
    the stream has started arrive as an `error` event (the 200 is already on
    the wire by then); failures before that are normal HTTP errors."""
    nbm = _get_notebook(nb)
    try:
        events = await service.ask_stream(
            nbm,
            body.question,
            config=_config,
            embedder=_embedder,
            filter=body.filter,
            max_tiddlers=body.max_tiddlers,
            history=[t.model_dump() for t in body.history],
        )
    except service.AskError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return _sse_response(events)


@app.post("/notebooks/{nb}/generate", dependencies=[Depends(require_auth)])
async def generate(nb: str, body: GenerateBody):
    """One-shot generation command over a single tiddler (summarize / tags /
    title / tasks) — render → generate, no embedding round-trip."""
    nbm = _get_notebook(nb)
    try:
        return await service.generate(
            nbm, body.title, body.command, config=_config
        )
    except service.AskError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/notebooks/{nb}/digest", dependencies=[Depends(require_auth)])
async def digest(nb: str, body: DigestBody | None = None):
    """Synthesize a what-changed digest now (same code path the scheduler
    runs). Body is optional: {filter, title, write} override the defaults."""
    body = body or DigestBody()
    nbm = _get_notebook(nb)
    try:
        return await service.digest(
            nbm,
            config=_config,
            filter=body.filter,
            title=body.title,
            write=body.write,
        )
    except service.AskError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- client-supplied-content routes (local mode) ---
#
# Top-level (no notebook in the path): the caller sends the note content, so
# these work for wikis the gateway has no config/credentials for. No Playwright
# involvement anywhere below this line.


@app.post("/notes/check", dependencies=[Depends(require_auth)])
async def notes_check(body: NotesCheckBody):
    """Pre-flight for local-mode requests: which of these note hashes must be
    sent in full? Known hashes get their retention window bumped."""
    present = _note_cache.check(body.hashes)
    return {"missing": [h for h in body.hashes if h not in present]}


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
    """SSE variant of the client-content /ask; same event framing as the
    notebook route. Unknown hash refs 409 before the stream starts."""
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
