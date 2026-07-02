import secrets
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import parse_qs

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from google.genai import errors as genai_errors
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from .ai import answer_question
from .config import AppConfig, load_config
from .embeddings import Embedder
from .manager import AppManager
from .mcp_server import init as mcp_init
from .mcp_server import mcp

_config: AppConfig | None = None
_manager: AppManager | None = None
_embedder: Embedder | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _manager, _embedder
    _config = load_config()
    _manager = AppManager()
    await _manager.start(_config.notebooks, _config.profiles_dir)
    _embedder = Embedder(_config.ollama_url, _config.embed_model)
    mcp_init(_config, _manager, _embedder)
    yield
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


class AskBody(BaseModel):
    question: str
    filter: Optional[str] = None
    # Bounds embedding-cache *misses* per request (cost control), not the
    # candidate pool — every filter match participates in ranking once cached.
    max_tiddlers: int = Field(100, ge=1, le=1000)


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


@app.post("/notebooks/{nb}/ask", dependencies=[Depends(require_auth)])
async def ask(nb: str, body: AskBody):
    if not _config.gemini_api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY not configured")
    nbm = _get_notebook(nb)
    filter_str = body.filter or "[!is[system]]"
    tiddlers = await nbm.filter_tiddlers(filter_str, full=True)
    try:
        return await answer_question(
            question=body.question,
            tiddlers=tiddlers,
            embedder=_embedder,
            top_k=_config.rag_top_k,
            api_key=_config.gemini_api_key,
            model=_config.gemini_model,
            max_embed=body.max_tiddlers,
        )
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Cannot reach the embedding service — Ollama may still be starting up.")
    except httpx.TimeoutException:
        raise HTTPException(status_code=503, detail="Upstream request timed out — the embedding batch may be large. Please try again.")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=503, detail="Embedding model not ready — it may still be downloading. Please wait and try again.")
        raise HTTPException(status_code=503, detail=f"Embedding service returned {e.response.status_code}.")
    except genai_errors.ServerError:
        raise HTTPException(status_code=503, detail="The AI model is busy right now. Please try again in a moment.")
    except genai_errors.ClientError as e:
        raise HTTPException(status_code=502, detail=f"AI model error: {e.message}")
    except Exception as e:
        # Re-raise as HTTPException so it flows through CORSMiddleware —
        # unhandled exceptions bypass it and the browser sees a CORS error.
        raise HTTPException(status_code=500, detail=str(e))
