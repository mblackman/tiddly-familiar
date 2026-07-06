"""
AI services over client-supplied note content, shared by the REST routes.

Owns the rank → generate sequences and translates every backend failure
(Ollama, Gemini transport, Gemini API) into an AskError with a user-facing
message. All note content arrives from the caller — the server never fetches
from a wiki itself.
"""

import httpx
from google.genai import errors as genai_errors

from .ai import COMMANDS, GenerationError, answer_question, answer_question_stream
from .ai import related as ai_related
from .ai import run_command
from .ai import search as ai_search
from .embeddings import EmbeddingError


class AskError(RuntimeError):
    """Service failure with a message safe to show the caller.

    `status` is the HTTP status the REST route should map it to.
    """

    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.status = status


def _translate(e: Exception) -> AskError:
    """Map a generation/embedding backend failure to an AskError."""
    if isinstance(e, (EmbeddingError, GenerationError)):
        return AskError(str(e), 503)
    if isinstance(e, (httpx.ConnectError, httpx.TimeoutException)):
        # Embedder httpx errors are wrapped in EmbeddingError and local-LLM
        # ones in GenerationError, so anything reaching here came from the
        # Gemini client's transport.
        return AskError(
            "Cannot reach the AI model service — network issue toward Gemini. "
            "Please try again.",
            503,
        )
    if isinstance(e, genai_errors.ServerError):
        return AskError(
            "The AI model is busy right now. Please try again in a moment.", 503
        )
    if isinstance(e, genai_errors.ClientError):
        return AskError(f"AI model error: {e.message}", 502)
    raise e


def _check_generation_backend(config) -> None:
    if config.llm_backend == "gemini" and not config.gemini_api_key:
        raise AskError("GEMINI_API_KEY not configured", 503)


async def ask_with_tiddlers(
    question: str,
    tiddlers: list[dict],
    *,
    config,
    embedder,
    max_tiddlers: int = 100,
    history: list[dict] | None = None,
) -> dict:
    """Answer a question over the supplied candidate list. Returns
    {answer, sources, truncated}.

    `max_tiddlers` bounds embedding-cache *misses* per request (cost control),
    not the candidate pool; it is clamped to >= 1 here because max_new <= 0
    would skip every uncached tiddler forever. `history` is prior chat turns
    [{role, content}].
    """
    _check_generation_backend(config)
    try:
        return await answer_question(
            question=question,
            tiddlers=tiddlers,
            embedder=embedder,
            cfg=config,
            max_embed=max(1, max_tiddlers),
            history=history,
        )
    except Exception as e:
        raise _translate(e) from e


async def ask_stream_with_tiddlers(
    question: str,
    tiddlers: list[dict],
    *,
    config,
    embedder,
    max_tiddlers: int = 100,
    history: list[dict] | None = None,
):
    """Streaming `ask_with_tiddlers`. Guard failures raise AskError from this
    coroutine (before any bytes are sent); the returned async generator yields
    (event, data) pairs: ("delta", {text}) fragments, one final ("done",
    {answer, sources, truncated}) — or ("error", {message, status}) if a
    backend fails after the stream has started."""
    _check_generation_backend(config)

    async def events():
        try:
            async for event in answer_question_stream(
                question=question,
                tiddlers=tiddlers,
                embedder=embedder,
                cfg=config,
                max_embed=max(1, max_tiddlers),
                history=history,
            ):
                yield event
        except Exception as e:
            err = _translate(e)
            yield ("error", {"message": str(err), "status": err.status})

    return events()


async def related_with_tiddlers(
    target: dict,
    tiddlers: list[dict],
    *,
    embedder,
    k: int = 5,
    max_tiddlers: int = 100,
) -> dict:
    """Candidates most similar to `target` ({title, text, fields}) by
    embedding similarity — no generation model involved. Returns
    {related: [{title, score}], truncated}."""
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


async def search_with_tiddlers(
    query: str,
    tiddlers: list[dict],
    *,
    embedder,
    k: int = 10,
    max_tiddlers: int = 100,
) -> dict:
    """Ranked semantic search over the supplied candidates — no generation
    model involved. Returns {results: [{title, score, snippet}], truncated}."""
    try:
        items, truncated = await ai_search(
            query,
            tiddlers,
            embedder,
            top_k=max(1, k),
            max_embed=max(1, max_tiddlers),
        )
    except EmbeddingError as e:
        raise AskError(str(e), 503) from e
    return {"results": items, "truncated": truncated}


def _parse_tags(result: str) -> list[str]:
    """One tag per model output line → clean tag list (bullets/quotes the
    model sneaked in stripped, blanks dropped, capped at 5)."""
    tags = []
    for line in result.splitlines():
        tag = line.strip().lstrip("-*#").strip().strip("\"'").strip()
        if tag:
            tags.append(tag)
    return tags[:5]


async def generate_with_text(
    title: str,
    text: str,
    command: str,
    *,
    config,
    vocabulary: list[str] | None = None,
) -> dict:
    """One-shot generation command over already-rendered plain text
    (summarize / tags / title / tasks) — no embedding round-trip.
    Returns {command, title, result} plus `tags` (parsed list) for the tags
    command. `vocabulary` seeds the tags command with existing tag names.
    """
    if command not in COMMANDS:
        raise AskError(
            f"Unknown command '{command}' — expected one of: "
            f"{', '.join(sorted(COMMANDS))}",
            400,
        )
    _check_generation_backend(config)
    text = (text or "").strip()
    if not text:
        raise AskError(f"Tiddler '{title}' has no text to work with", 422)

    try:
        result = await run_command(command, title, text, config, vocabulary)
    except Exception as e:
        raise _translate(e) from e

    out = {"command": command, "title": title, "result": result}
    if command == "tags":
        out["tags"] = _parse_tags(result)
    return out
