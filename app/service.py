"""
Shared notebook-AI services used by both transports (REST routes and MCP tools).

Owns the fetch → rank → generate sequences and translates every backend
failure (Ollama, Gemini transport, Gemini API) into an AskError with a
user-facing message, so the two transports can't diverge in behaviour.
"""

from datetime import date

import httpx
from google.genai import errors as genai_errors

from .ai import COMMANDS, GenerationError, answer_question, answer_question_stream
from .ai import digest_text as ai_digest_text
from .ai import related as ai_related
from .ai import run_command
from .embeddings import EmbeddingError

DEFAULT_FILTER = "[!is[system]]"

DIGEST_TAG = "ai-digest"


class AskError(RuntimeError):
    """Service failure with a message safe to show the caller.

    `status` is the HTTP status the REST route should map it to; MCP clients
    just see the message (FastMCP surfaces the exception text as a tool error).
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
    """Answer a question over an explicit candidate list. Returns
    {answer, sources, truncated}. Core shared by the notebook path (which
    fetches the list from the wiki) and the client-supplied-content routes
    (which receive it in the request body).

    `max_tiddlers` bounds embedding-cache *misses* per request (cost control),
    not the candidate pool; it is clamped to >= 1 here because MCP arguments
    aren't range-validated and max_new <= 0 would skip every uncached tiddler
    forever. `history` is prior chat turns [{role, content}].
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


async def ask(
    nbm,
    question: str,
    *,
    config,
    embedder,
    filter: str | None = None,
    max_tiddlers: int = 100,
    history: list[dict] | None = None,
) -> dict:
    """Answer a question about a notebook: fetch the filter matches, then
    delegate to `ask_with_tiddlers`."""
    _check_generation_backend(config)
    tiddlers = await nbm.filter_tiddlers(filter or DEFAULT_FILTER, full=True)
    return await ask_with_tiddlers(
        question,
        tiddlers,
        config=config,
        embedder=embedder,
        max_tiddlers=max_tiddlers,
        history=history,
    )


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


async def ask_stream(
    nbm,
    question: str,
    *,
    config,
    embedder,
    filter: str | None = None,
    max_tiddlers: int = 100,
    history: list[dict] | None = None,
):
    """Streaming `ask`: fetch the filter matches (failures there raise
    AskError before any bytes are sent), then delegate to
    `ask_stream_with_tiddlers`."""
    _check_generation_backend(config)
    tiddlers = await nbm.filter_tiddlers(filter or DEFAULT_FILTER, full=True)
    return await ask_stream_with_tiddlers(
        question,
        tiddlers,
        config=config,
        embedder=embedder,
        max_tiddlers=max_tiddlers,
        history=history,
    )


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
    """Notebook variant of `related_with_tiddlers`: resolve `title` and the
    candidate pool from the wiki, then delegate."""
    target = await nbm.get_tiddler(title)
    if target is None:
        raise AskError(f"Tiddler '{title}' not found", 404)
    tiddlers = await nbm.filter_tiddlers(filter or DEFAULT_FILTER, full=True)
    return await related_with_tiddlers(
        target,
        tiddlers,
        embedder=embedder,
        k=k,
        max_tiddlers=max_tiddlers,
    )


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
    """One-shot generation command over already-resolved plain text
    (summarize / tags / title / tasks) — no embedding round-trip. Core shared
    by the notebook path (which renders the tiddler server-side) and the
    client-supplied-content route (which receives browser-rendered text).
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


async def generate(
    nbm,
    title: str,
    command: str,
    *,
    config,
) -> dict:
    """Notebook variant of `generate_with_text`: resolve the tiddler, render
    it to plain text server-side, then delegate."""
    if command not in COMMANDS:
        raise AskError(
            f"Unknown command '{command}' — expected one of: "
            f"{', '.join(sorted(COMMANDS))}",
            400,
        )
    _check_generation_backend(config)
    tiddler = await nbm.get_tiddler(title)
    if tiddler is None:
        raise AskError(f"Tiddler '{title}' not found", 404)

    # Rendered plain text resolves transclusions/macros; fall back to the raw
    # text when rendering yields nothing (e.g. a data tiddler).
    text = (await nbm.render(title, mode="plain") or "").strip()
    if not text:
        text = tiddler.get("text", "").strip()

    vocabulary = None
    if command == "tags":
        vocabulary = await nbm.filter_tiddlers("[tags[]]")

    return await generate_with_text(
        title, text, command, config=config, vocabulary=vocabulary
    )


async def digest(
    nbm,
    *,
    config,
    filter: str | None = None,
    title: str | None = None,
    write: bool = True,
) -> dict:
    """Synthesize a what-changed digest and (by default) write it back as a
    tiddler tagged `ai-digest`. Returns {written, title, sources, text} — or
    {written: False, reason} when nothing changed in the period."""
    _check_generation_backend(config)
    tiddlers = await nbm.filter_tiddlers(filter or config.digest_filter, full=True)
    tiddlers = [t for t in tiddlers if t.get("text", "").strip()]
    if not tiddlers:
        return {"written": False, "reason": "no recently modified notes"}

    try:
        text, sources = await ai_digest_text(tiddlers, config)
    except Exception as e:
        raise _translate(e) from e

    digest_title = title or f"AI Digest {date.today().isoformat()}"
    if write:
        await nbm.put_tiddler(digest_title, {"tags": DIGEST_TAG}, text)
    return {"written": write, "title": digest_title, "sources": sources, "text": text}
