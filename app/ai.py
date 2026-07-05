import json
import re

import httpx
import numpy as np
from google import genai
from google.genai import types

from .embeddings import Embedder, rank

# Local generation is CPU-bound and can legitimately take minutes.
_OLLAMA_GEN_TIMEOUT = 300.0

# Rolling chat history sent as prior turns. Old turns carry the least signal,
# so trimming drops from the front; the char budget stops a few huge answers
# from crowding the actual notes out of the model's attention.
_MAX_HISTORY_TURNS = 10
_HISTORY_CHAR_BUDGET = 8000


class GenerationError(RuntimeError):
    """Local-LLM backend failure, with a message safe to show to the caller."""

# --- retrieval tuning ---

# Long tiddlers are embedded and scored as overlapping chunks so a relevant
# tail can't hide behind an irrelevant head. Chunks land in the same
# content-hash cache as whole short tiddlers.
_CHUNK_SIZE = 2000
_CHUNK_OVERLAP = 200

# Generation-prompt budgets (chars). Cosine ranking already ordered the notes,
# so when the budget runs out it's the least relevant notes that get dropped.
_TIDDLER_CONTEXT_BUDGET = 4000
_TOTAL_CONTEXT_BUDGET = 24000

# Hybrid retrieval: cosine similarity (roughly 0..1) plus a keyword bonus
# (0..1) — exact terms, titles, and tags are wiki staples that pure embedding
# similarity is weak on.
_KEYWORD_WEIGHT = 0.35

_STOPWORDS = frozenset(
    """a about an and are as at be but by can could do does for from had has
    have how i if in is it its me my not of on or our so that the their there
    these they this to was we were what when where which who why will with
    would you your""".split()
)


def _title(t: dict) -> str:
    return t.get("title") or t.get("fields", {}).get("title", "")


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _query_terms(question: str) -> set[str]:
    """Content-bearing words of the question (stopwords and 1–2 letter tokens
    carry no retrieval signal)."""
    return {t for t in _tokens(question) if len(t) >= 3 and t not in _STOPWORDS}


def _keyword_score(terms: set[str], t: dict) -> float:
    """0..1: fraction of query terms found in the tiddler, with title/tag hits
    worth double a body hit."""
    if not terms:
        return 0.0
    fields = t.get("fields", {})
    head = _tokens(f"{_title(t)} {fields.get('tags', '')}")
    body = _tokens(t.get("text", ""))
    score = 0.0
    for term in terms:
        if term in head:
            score += 2.0
        elif term in body:
            score += 1.0
    return score / (2.0 * len(terms))


def _chunk_text(text: str) -> list[tuple[int, str]]:
    """Split into overlapping (offset, chunk) windows. Text at most one chunk
    long comes back whole, so short tiddlers keep their existing cache hashes."""
    if len(text) <= _CHUNK_SIZE:
        return [(0, text)]
    step = _CHUNK_SIZE - _CHUNK_OVERLAP
    chunks = []
    start = 0
    while True:
        chunks.append((start, text[start : start + _CHUNK_SIZE]))
        if start + _CHUNK_SIZE >= len(text):
            return chunks
        start += step


def _embed_text(title: str, text: str) -> str:
    """Embeddable representation: the title carries signal, so prepend it.
    Format must stay stable — it is the content-hash key of the persistent
    embedding cache."""
    return f"{title}\n{text}".strip()


def _excerpt(text: str, chunks: list[tuple[int, str, float]]) -> str:
    """Budgeted excerpt of a long tiddler: its best-scoring chunks, merged
    where they overlap, re-joined in document order."""
    if len(text) <= _TIDDLER_CONTEXT_BUDGET:
        return text
    spans: list[tuple[int, int]] = []
    used = 0
    for offset, chunk, _score in sorted(chunks, key=lambda c: -c[2]):
        if used >= _TIDDLER_CONTEXT_BUDGET:
            break
        spans.append((offset, offset + len(chunk)))
        used += len(chunk)
    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return "\n[...]\n".join(text[s:e] for s, e in merged)


async def _score_candidate_chunks(
    query_vec: list[float],
    candidates: list[dict],
    embedder: Embedder,
    max_embed: int | None,
) -> tuple[list[tuple[int, int, str]], list[float], bool]:
    """Chunk every candidate, embed the chunks, cosine-score them against the
    query. Returns (records, cos, truncated) where records[i] is
    (candidate index, offset, chunk) and cos[i] its score — 0.0 for chunks
    whose vector wasn't computed yet (see `max_embed`)."""
    records = [
        (ci, offset, chunk)
        for ci, t in enumerate(candidates)
        for offset, chunk in _chunk_text(t.get("text", ""))
    ]
    embed_texts = [_embed_text(_title(candidates[ci]), chunk) for ci, _o, chunk in records]

    vecs = await embedder.embed_documents(embed_texts, max_new=max_embed)
    truncated = any(v is None for v in vecs)

    cos = [0.0] * len(records)
    embedded = [(i, v) for i, v in enumerate(vecs) if v is not None]
    for rank_idx, score in rank(query_vec, [v for _i, v in embedded]):
        cos[embedded[rank_idx][0]] = score
    return records, cos, truncated


async def retrieve(
    question: str,
    tiddlers: list[dict],
    embedder: Embedder,
    top_k: int,
    max_embed: int | None = None,
) -> tuple[list[dict], bool]:
    """Hybrid chunked retrieval. Returns (selected, truncated) where selected
    is the top-k tiddlers as [{title, text}] — text already excerpted to the
    per-tiddler budget — ordered most relevant first.

    Every chunk of every candidate is cosine-scored against the question
    (chunks without a cached vector score 0 there, see `max_embed` in
    `Embedder.embed_documents`), then the parent tiddler's keyword overlap is
    added, so an exact title/tag/term match surfaces even before its
    embeddings are warm. A tiddler ranks by its best chunk.
    """
    candidates = [t for t in tiddlers if t.get("text", "").strip()]
    if not candidates:
        return [], False

    query_vec = await embedder.embed_query(question)
    records, cos, truncated = await _score_candidate_chunks(
        query_vec, candidates, embedder, max_embed
    )

    terms = _query_terms(question)
    kw = [_keyword_score(terms, t) for t in candidates]

    best: dict[int, float] = {}
    scored_chunks: dict[int, list[tuple[int, str, float]]] = {}
    for i, (ci, offset, chunk) in enumerate(records):
        score = cos[i] + _KEYWORD_WEIGHT * kw[ci]
        best[ci] = max(best.get(ci, score), score)
        scored_chunks.setdefault(ci, []).append((offset, chunk, score))

    top = sorted(best, key=lambda ci: -best[ci])[:top_k]
    selected = [
        {
            "title": _title(candidates[ci]),
            "text": _excerpt(candidates[ci].get("text", ""), scored_chunks[ci]),
        }
        for ci in top
    ]
    return selected, truncated


async def related(
    target: dict,
    tiddlers: list[dict],
    embedder: Embedder,
    top_k: int,
    max_embed: int | None = None,
) -> tuple[list[dict], bool]:
    """Tiddlers most similar to `target`, by embedding similarity alone.
    Returns ([{title, score}], truncated), best first, zero-similarity
    candidates dropped.

    The query vector is the mean of the target's chunk vectors (embedded
    without a miss budget — in the common case they're already cached from a
    prior ask). Candidates rank by their best chunk, like `retrieve`.
    """
    t_title = _title(target)
    text = target.get("text", "")
    if not text.strip():
        return [], False
    target_vecs = [
        v
        for v in await embedder.embed_documents(
            [_embed_text(t_title, chunk) for _o, chunk in _chunk_text(text)]
        )
        if v is not None
    ]
    query_vec = np.mean(np.asarray(target_vecs, dtype=np.float32), axis=0).tolist()

    candidates = [
        t for t in tiddlers if t.get("text", "").strip() and _title(t) != t_title
    ]
    if not candidates:
        return [], False
    records, cos, truncated = await _score_candidate_chunks(
        query_vec, candidates, embedder, max_embed
    )

    best: dict[int, float] = {}
    for i, (ci, _offset, _chunk) in enumerate(records):
        best[ci] = max(best.get(ci, cos[i]), cos[i])

    top = sorted((ci for ci in best if best[ci] > 0), key=lambda ci: -best[ci])
    return [
        {"title": _title(candidates[ci]), "score": round(best[ci], 4)}
        for ci in top[:top_k]
    ], truncated


def _build_context(tiddlers: list[dict]) -> tuple[str, list[str]]:
    """Assemble the generation context, stopping at the total budget — input
    is ranked best-first, so overflow drops the least relevant notes."""
    parts = []
    sources = []
    total = 0
    for t in tiddlers:
        title = _title(t)
        text = t.get("text", "").strip()
        if not text:
            continue
        if parts and total + len(text) > _TOTAL_CONTEXT_BUDGET:
            break
        parts.append(f"## {title}\n{text}")
        sources.append(title)
        total += len(text)
    return "\n\n".join(parts), sources


_SYSTEM_INSTRUCTION = (
    "You are answering questions about a personal TiddlyWiki notebook. "
    "Use only the provided notes to answer. "
    "Format the answer in Markdown. "
    "When you reference a specific note, cite it inline using TiddlyWiki link "
    "syntax: [[Note Title]] — use the exact title as given in the notes. "
    "If the notes don't contain enough information, say so."
)


def _trim_history(history: list[dict] | None) -> list[dict]:
    """The most recent chat turns that fit the turn/char budgets. The newest
    turn always survives — it may hold the antecedent of a follow-up question."""
    turns = [
        t
        for t in (history or [])
        if t.get("content") and t.get("role") in ("user", "assistant")
    ][-_MAX_HISTORY_TURNS:]
    kept: list[dict] = []
    used = 0
    for t in reversed(turns):
        used += len(t["content"])
        if kept and used > _HISTORY_CHAR_BUDGET:
            break
        kept.append(t)
    return list(reversed(kept))


def _ollama_messages(system: str, prompt: str, history: list[dict] | None) -> list[dict]:
    return (
        [{"role": "system", "content": system}]
        + [{"role": t["role"], "content": t["content"]} for t in _trim_history(history)]
        + [{"role": "user", "content": prompt}]
    )


def _gemini_contents(prompt: str, history: list[dict] | None) -> list:
    contents = [
        types.Content(
            role="user" if t["role"] == "user" else "model",
            parts=[types.Part(text=t["content"])],
        )
        for t in _trim_history(history)
    ]
    contents.append(types.Content(role="user", parts=[types.Part(text=prompt)]))
    return contents


def _build_prompt(question: str, tiddlers: list[dict]) -> tuple[str, list[str]]:
    context, titles = _build_context(tiddlers)
    return f"Here are the relevant notes:\n\n{context}\n\nQuestion: {question}", titles


async def _generate_text(
    system: str, prompt: str, cfg, history: list[dict] | None = None
) -> str:
    """Dispatch one generation to the configured backend (Gemini or Ollama)."""
    if cfg.llm_backend == "ollama":
        return await _generate_ollama(
            prompt, cfg.ollama_url, cfg.ollama_llm_model, system=system, history=history
        )
    return await _generate_gemini(
        prompt, cfg.gemini_api_key, cfg.gemini_model, system=system, history=history
    )


async def _generate(
    question: str, tiddlers: list[dict], cfg, history: list[dict] | None = None
) -> dict:
    """Send the (already-ranked) tiddlers to the configured generation backend
    and return {answer, sources}. Sources are derived from [[Note Title]]
    citations in the answer text."""
    prompt, available_titles = _build_prompt(question, tiddlers)
    answer = await _generate_text(_SYSTEM_INSTRUCTION, prompt, cfg, history)
    answer = answer or "The AI model returned no answer for this question."
    return {"answer": answer, "sources": _extract_citations(answer, set(available_titles))}


async def _generate_gemini(
    prompt: str,
    api_key: str,
    model: str,
    system: str = _SYSTEM_INSTRUCTION,
    history: list[dict] | None = None,
) -> str:
    client = genai.Client(api_key=api_key)
    response = await client.aio.models.generate_content(
        model=model,
        contents=_gemini_contents(prompt, history),
        config=types.GenerateContentConfig(system_instruction=system),
    )
    # response.text is Optional — None when the response has no text parts
    # (e.g. blocked by safety filters or an empty candidate).
    return response.text or ""


async def _stream_gemini(
    prompt: str,
    api_key: str,
    model: str,
    system: str = _SYSTEM_INSTRUCTION,
    history: list[dict] | None = None,
):
    client = genai.Client(api_key=api_key)
    stream = await client.aio.models.generate_content_stream(
        model=model,
        contents=_gemini_contents(prompt, history),
        config=types.GenerateContentConfig(system_instruction=system),
    )
    async for chunk in stream:
        if chunk.text:
            yield chunk.text


def _ollama_error(exc: httpx.HTTPError, model: str) -> GenerationError:
    """Map an httpx failure toward Ollama to a message safe to show the caller."""
    if isinstance(exc, httpx.ConnectError):
        return GenerationError(
            "Cannot reach the local LLM service — Ollama may still be starting up."
        )
    if isinstance(exc, httpx.TimeoutException):
        return GenerationError(
            "The local LLM timed out — it may be busy or the model too large "
            "for this host. Please try again."
        )
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 404:
            return GenerationError(
                f"Local LLM model '{model}' is not available — it may still be "
                "downloading. Please wait and try again."
            )
        return GenerationError(f"Local LLM service returned {exc.response.status_code}.")
    return GenerationError(f"Local LLM request failed: {exc}")


async def _generate_ollama(
    prompt: str,
    ollama_url: str,
    model: str,
    system: str = _SYSTEM_INSTRUCTION,
    history: list[dict] | None = None,
) -> str:
    """Fully-local generation via Ollama's /api/chat. Failures surface as
    GenerationError so callers can distinguish them from Gemini problems."""
    payload = {
        "model": model,
        "messages": _ollama_messages(system, prompt, history),
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=_OLLAMA_GEN_TIMEOUT) as client:
            resp = await client.post(f"{ollama_url.rstrip('/')}/api/chat", json=payload)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise _ollama_error(e, model) from e
    return (resp.json().get("message") or {}).get("content", "")


async def _stream_ollama(
    prompt: str,
    ollama_url: str,
    model: str,
    system: str = _SYSTEM_INSTRUCTION,
    history: list[dict] | None = None,
):
    """Streaming twin of _generate_ollama: /api/chat with stream=true yields
    NDJSON lines, one message fragment each."""
    payload = {
        "model": model,
        "messages": _ollama_messages(system, prompt, history),
        "stream": True,
    }
    try:
        async with httpx.AsyncClient(timeout=_OLLAMA_GEN_TIMEOUT) as client:
            async with client.stream(
                "POST", f"{ollama_url.rstrip('/')}/api/chat", json=payload
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise GenerationError(f"Local LLM error: {data['error']}")
                    text = (data.get("message") or {}).get("content", "")
                    if text:
                        yield text
                    if data.get("done"):
                        return
    except httpx.HTTPError as e:
        raise _ollama_error(e, model) from e


def _extract_citations(answer: str, title_set: set[str]) -> list[str]:
    """Titles cited as [[Title]] or [[display text|Title]] in the answer,
    filtered to titles that were in the context (avoids hallucinated links),
    deduped in first-citation order. TiddlyWiki link targets are everything
    after the first pipe."""
    targets = (
        m.split("|", 1)[-1] for m in re.findall(r"\[\[([^\]]+)\]\]", answer)
    )
    return list(dict.fromkeys(t for t in targets if t in title_set))


async def answer_question(
    question: str,
    tiddlers: list[dict],
    embedder: Embedder,
    cfg,
    max_embed: int | None = None,
    history: list[dict] | None = None,
) -> dict:
    """RAG orchestrator: hybrid-rank the candidate tiddlers (see `retrieve`),
    keep the top rag_top_k, and hand budgeted excerpts to the configured
    generation backend (`cfg.llm_backend`: Gemini or local Ollama).

    `max_embed` only bounds how many embedding-cache *misses* are computed this
    request. Skipped misses are cached on later requests, so coverage converges
    to the full corpus. Returns {answer, sources, truncated} — `truncated` is
    True while some chunks were skipped (i.e. the answer may not have seen
    everything yet).

    `history` is prior chat turns as [{role: user|assistant, content}] —
    retrieval ranks by the current question alone; history only shapes the
    generated answer (follow-ups, pronouns).
    """
    selected, truncated = await retrieve(
        question, tiddlers, embedder, cfg.rag_top_k, max_embed=max_embed
    )
    if not selected:
        return {
            "answer": "There are no notes matching that filter to answer from.",
            "sources": [],
            "truncated": False,
        }
    result = await _generate(question, selected, cfg, history)
    result["truncated"] = truncated
    return result


async def answer_question_stream(
    question: str,
    tiddlers: list[dict],
    embedder: Embedder,
    cfg,
    max_embed: int | None = None,
    history: list[dict] | None = None,
):
    """Streaming twin of `answer_question`. Retrieval is identical; only the
    generation call streams. Yields ("delta", {text}) per model fragment, then
    one final ("done", {answer, sources, truncated}) with the assembled answer
    (sources need the full text, so they can only be extracted at the end)."""
    selected, truncated = await retrieve(
        question, tiddlers, embedder, cfg.rag_top_k, max_embed=max_embed
    )
    if not selected:
        yield (
            "done",
            {
                "answer": "There are no notes matching that filter to answer from.",
                "sources": [],
                "truncated": False,
            },
        )
        return

    prompt, available_titles = _build_prompt(question, selected)
    if cfg.llm_backend == "ollama":
        stream = _stream_ollama(
            prompt, cfg.ollama_url, cfg.ollama_llm_model, history=history
        )
    else:
        stream = _stream_gemini(
            prompt, cfg.gemini_api_key, cfg.gemini_model, history=history
        )

    parts: list[str] = []
    async for text in stream:
        parts.append(text)
        yield ("delta", {"text": text})

    answer = "".join(parts) or "The AI model returned no answer for this question."
    yield (
        "done",
        {
            "answer": answer,
            "sources": _extract_citations(answer, set(available_titles)),
            "truncated": truncated,
        },
    )


# --- one-shot generation commands (no retrieval) ---

# Each command is a prompt template over a single note's rendered text —
# straight to the generation backend, skipping the embed/rank round-trip.
_COMMAND_INPUT_BUDGET = 24000

_COMMANDS: dict[str, dict] = {
    "summarize": {
        "system": (
            "You summarize notes from a personal TiddlyWiki notebook. "
            "Reply with only the summary in Markdown — no preamble, no headings."
        ),
        "prompt": "Summarize the following note in 2-3 sentences.\n\n# {title}\n\n{text}",
    },
    "tags": {
        "system": (
            "You suggest tags for notes in a personal TiddlyWiki notebook. "
            "Reply with one tag per line, at most 5 lines, no bullets or "
            "commentary. Prefer reusing tags from the existing vocabulary when "
            "they fit; only invent a new tag when nothing existing applies. "
            "Tags may contain spaces."
        ),
        "prompt": (
            "Existing tags in this wiki: {vocabulary}\n\n"
            "Suggest tags for the following note.\n\n# {title}\n\n{text}"
        ),
    },
    "title": {
        "system": (
            "You write titles for notes in a personal TiddlyWiki notebook. "
            "Reply with a single concise title of at most 60 characters — "
            "no quotes, no commentary."
        ),
        "prompt": "Suggest a title for the following note.\n\n{text}",
    },
    "tasks": {
        "system": (
            "You extract open action items from notes in a personal TiddlyWiki "
            "notebook. Reply with a Markdown bullet list, one task per line, "
            "most urgent first. If there are no action items, reply exactly: "
            "(no tasks found)"
        ),
        "prompt": "Extract the action items from the following note.\n\n# {title}\n\n{text}",
    },
}

COMMANDS = frozenset(_COMMANDS)


async def run_command(
    command: str,
    title: str,
    text: str,
    cfg,
    vocabulary: list[str] | None = None,
) -> str:
    """One-shot transform of a single note. `vocabulary` (the wiki's existing
    tags) only feeds the 'tags' command."""
    spec = _COMMANDS[command]
    prompt = spec["prompt"].format(
        title=title,
        text=text[:_COMMAND_INPUT_BUDGET],
        vocabulary=", ".join(vocabulary or []) or "(none yet)",
    )
    return (await _generate_text(spec["system"], prompt, cfg)).strip()
