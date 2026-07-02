"""
Shared notebook-AI services used by both transports (REST routes and MCP tools).

Owns the fetch → rank → generate sequences and translates every backend
failure (Ollama, Gemini transport, Gemini API) into an AskError with a
user-facing message, so the two transports can't diverge in behaviour.
"""

import httpx
from google.genai import errors as genai_errors

from .ai import GenerationError, answer_question
from .ai import related as ai_related
from .embeddings import EmbeddingError

DEFAULT_FILTER = "[!is[system]]"


class AskError(RuntimeError):
    """Service failure with a message safe to show the caller.

    `status` is the HTTP status the REST route should map it to; MCP clients
    just see the message (FastMCP surfaces the exception text as a tool error).
    """

    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.status = status


async def ask(
    nbm,
    question: str,
    *,
    config,
    embedder,
    filter: str | None = None,
    max_tiddlers: int = 100,
) -> dict:
    """Answer a question about a notebook. Returns {answer, sources, truncated}.

    `max_tiddlers` bounds embedding-cache *misses* per request (cost control),
    not the candidate pool; it is clamped to >= 1 here because MCP arguments
    aren't range-validated and max_new <= 0 would skip every uncached tiddler
    forever.
    """
    if config.llm_backend == "gemini" and not config.gemini_api_key:
        raise AskError("GEMINI_API_KEY not configured", 503)
    tiddlers = await nbm.filter_tiddlers(filter or DEFAULT_FILTER, full=True)
    try:
        return await answer_question(
            question=question,
            tiddlers=tiddlers,
            embedder=embedder,
            cfg=config,
            max_embed=max(1, max_tiddlers),
        )
    except EmbeddingError as e:
        raise AskError(str(e), 503) from e
    except GenerationError as e:
        raise AskError(str(e), 503) from e
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        # Embedder httpx errors are wrapped in EmbeddingError and local-LLM
        # ones in GenerationError, so anything reaching here came from the
        # Gemini client's transport.
        raise AskError(
            "Cannot reach the AI model service — network issue toward Gemini. "
            "Please try again.",
            503,
        ) from e
    except genai_errors.ServerError as e:
        raise AskError(
            "The AI model is busy right now. Please try again in a moment.", 503
        ) from e
    except genai_errors.ClientError as e:
        raise AskError(f"AI model error: {e.message}", 502) from e


async def related(
    nbm,
    title: str,
    *,
    config,
    embedder,
    k: int = 5,
    filter: str | None = None,
    max_tiddlers: int = 100,
) -> dict:
    """Tiddlers most similar to `title` by embedding similarity — no
    generation model involved. Returns {related: [{title, score}], truncated}.
    """
    target = await nbm.get_tiddler(title)
    if target is None:
        raise AskError(f"Tiddler '{title}' not found", 404)
    tiddlers = await nbm.filter_tiddlers(filter or DEFAULT_FILTER, full=True)
    try:
        items, truncated = await ai_related(
            target,
            tiddlers,
            embedder,
            top_k=max(1, k),
            max_embed=max(1, max_tiddlers),
        )
    except EmbeddingError as e:
        raise AskError(str(e), 503) from e
    return {"related": items, "truncated": truncated}
