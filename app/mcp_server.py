"""
MCP server exposing TiddlyWiki notebooks as Claude tools.

Mounted at /mcp on the FastAPI app. Configure in the Claude desktop app or
claude.ai as a remote MCP server pointing at http(s)://<host>:8787/mcp.
"""

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

from . import service

# Module-level globals set during FastAPI lifespan (see main.py)
_config = None
_manager = None
_embedder = None

# DNS rebinding protection is disabled: the gateway runs on an isolated Docker
# network and is not exposed to the internet, so the Host header check would
# only block legitimate clients (Claude Code via claude-docker hostname).
mcp = FastMCP(
    "TiddlyPWA Gateway",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


def init(config, manager, embedder) -> None:
    global _config, _manager, _embedder
    _config = config
    _manager = manager
    _embedder = embedder


def _nb(notebook: str):
    if _manager is None:
        raise RuntimeError("Gateway not initialized")
    try:
        return _manager.notebook(notebook)
    except KeyError:
        raise ValueError(f"Notebook '{notebook}' not configured")


@mcp.tool()
async def list_notebooks() -> list[str]:
    """List all configured TiddlyWiki notebook names."""
    if _config is None:
        return []
    return list(_config.notebooks.keys())


@mcp.tool()
async def search_tiddlers(
    notebook: str,
    filter: str = "[!is[system]]",
    full: bool = False,
) -> list:
    """
    Filter tiddlers using a TiddlyWiki filter expression and return matching tiddlers.

    Set full=true to include each tiddler's text and fields.
    Set full=false (default) to return titles only.

    Example filters:
      [!is[system]]            — all user tiddlers
      [tag[project]]           — tiddlers tagged 'project'
      [search[keyword]]        — full-text search
      [!is[system]!is[shadow]] — user tiddlers excluding shadows
    """
    nbm = _nb(notebook)
    return await nbm.filter_tiddlers(filter, full=full)


@mcp.tool()
async def get_tiddler(notebook: str, title: str) -> dict | None:
    """
    Get a single tiddler by exact title.
    Returns {title, fields, text} or null if not found.
    """
    nbm = _nb(notebook)
    return await nbm.get_tiddler(title)


@mcp.tool()
async def write_tiddler(
    notebook: str,
    title: str,
    text: str = "",
    fields: dict | None = None,
) -> bool:
    """
    Create or update a tiddler. Syncs to all connected devices automatically.

    Pass fields as a dict of TiddlyWiki field names (e.g. {"tags": "project meeting"}).
    The title and text fields are handled separately.
    """
    nbm = _nb(notebook)
    return await nbm.put_tiddler(title, fields or {}, text)


@mcp.tool()
async def delete_tiddler(notebook: str, title: str) -> bool:
    """Delete a tiddler by title. This syncs the deletion to all connected devices."""
    nbm = _nb(notebook)
    return await nbm.delete_tiddler(title)


@mcp.tool()
async def render_tiddler(
    notebook: str,
    title: str,
    as_format: str = "plain",
) -> str:
    """
    Render a tiddler's wikitext to plain text or HTML.
    Use as_format='plain' for reading/summarizing (best for AI use).
    Use as_format='html' when the rendered markup matters.
    """
    nbm = _nb(notebook)
    return await nbm.render(title, mode=as_format)


@mcp.tool()
async def related_tiddlers(
    notebook: str,
    title: str,
    k: int = 5,
    filter: str = "[!is[system]]",
) -> dict:
    """
    Find the tiddlers most similar to 'title' by local embedding similarity
    (no generation model involved — fast and fully local).

    Returns {related: [{title, score}], truncated} best-first; score is cosine
    similarity in 0..1. 'truncated' true means some candidates aren't embedded
    yet — repeat the call to make progress. 'filter' narrows the candidate
    pool, e.g. [tag[project]].
    """
    nbm = _nb(notebook)
    return await service.related(
        nbm, title, config=_config, embedder=_embedder, k=k, filter=filter
    )


@mcp.tool()
async def ask_notebook(
    notebook: str,
    question: str,
    filter: str = "[!is[system]]",
    max_tiddlers: int = 100,
) -> dict:
    """
    Answer a natural-language question about a notebook.

    Ranks every tiddler matching 'filter' by semantic similarity to the question
    using local embeddings, and passes only the most relevant top-k to Gemini.
    Returns {answer, sources, truncated} where 'sources' is the ranked set of
    tiddler titles actually used. max_tiddlers bounds how many *not-yet-cached*
    tiddlers get embedded per request (cost control); 'truncated' is true if
    some candidates were skipped because of it — repeating the question makes
    progress until the cache covers everything.

    'filter' pre-narrows the candidate pool; ranking handles final relevance, so
    a broad filter is fine, e.g.:
      [tag[project]]      — only project notes
      [search[meeting]]   — only notes matching 'meeting'
    """
    nbm = _nb(notebook)
    # service.AskError is a RuntimeError with a user-facing message; FastMCP
    # surfaces it to the client as the tool error text.
    return await service.ask(
        nbm,
        question,
        config=_config,
        embedder=_embedder,
        filter=filter,
        max_tiddlers=max_tiddlers,
    )
