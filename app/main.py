from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from .ai import ask_gemini
from .config import AppConfig, load_config
from .manager import AppManager
from .mcp_server import init as mcp_init
from .mcp_server import mcp

_config: AppConfig | None = None
_manager: AppManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _manager
    _config = load_config()
    _manager = AppManager()
    await _manager.start(_config.notebooks, _config.profiles_dir)
    mcp_init(_config, _manager)
    yield
    await _manager.stop()


app = FastAPI(title="TiddlyPWA Gateway", lifespan=lifespan)

# Mount the MCP server at /mcp (SSE transport for Claude desktop/web/mobile)
app.mount("/mcp", mcp.streamable_http_app())

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_auth(x_api_key: str = Depends(_api_key_header)):
    if not _config or x_api_key != _config.api_key:
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
    tiddlers = tiddlers[: body.max_tiddlers]
    return await ask_gemini(
        question=body.question,
        tiddlers=tiddlers,
        api_key=_config.gemini_api_key,
        model=_config.gemini_model,
    )
