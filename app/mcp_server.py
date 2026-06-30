"""
MCP server exposing TiddlyWiki notebooks as Claude tools.

Mounted at /mcp on the FastAPI app. Configure in the Claude desktop app or
claude.ai as a remote MCP server pointing at http(s)://<host>:8787/mcp.
"""

from mcp.server.fastmcp import FastMCP

from .ai import ask_gemini

# Module-level globals set during FastAPI lifespan (see main.py)
_config = None
_manager = None

mcp = FastMCP("TiddlyPWA Gateway")


def init(config, manager) -> None:
    global _config, _manager
    _config = config
    _manager = manager


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
async def ask_notebook(
    notebook: str,
    question: str,
    filter: str = "[!is[system]]",
    max_tiddlers: int = 100,
) -> dict:
    """
    Answer a natural-language question about a notebook using Gemini.

    Fetches tiddlers matching 'filter', passes them as context to Gemini,
    and returns the answer plus the list of source tiddler titles used.

    Use a narrower filter to stay within context limits, e.g.:
      [tag[project]]      — only project notes
      [search[meeting]]   — only notes matching 'meeting'
    """
    if _config is None or not _config.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")
    nbm = _nb(notebook)
    tiddlers = await nbm.filter_tiddlers(filter, full=True)
    tiddlers = tiddlers[:max_tiddlers]
    return await ask_gemini(
        question=question,
        tiddlers=tiddlers,
        api_key=_config.gemini_api_key,
        model=_config.gemini_model,
    )
