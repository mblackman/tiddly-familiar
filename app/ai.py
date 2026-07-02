import re

import httpx
import numpy as np
from google import genai
from google.genai import types

from .embeddings import Embedder, rank

# Local generation is CPU-bound and can legitimately take minutes.
_OLLAMA_GEN_TIMEOUT = 300.0


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


async def _generate(question: str, tiddlers: list[dict], cfg) -> dict:
    """Send the (already-ranked) tiddlers to the configured generation backend
    and return {answer, sources}. Sources are derived from [[Note Title]]
    citations in the answer text."""
    context, available_titles = _build_context(tiddlers)
    prompt = f"Here are the relevant notes:\n\n{context}\n\nQuestion: {question}"

    if cfg.llm_backend == "ollama":
        answer = await _generate_ollama(prompt, cfg.ollama_url, cfg.ollama_llm_model)
    else:
        answer = await _generate_gemini(prompt, cfg.gemini_api_key, cfg.gemini_model)

    answer = answer or "The AI model returned no answer for this question."
    return {"answer": answer, "sources": _extract_citations(answer, set(available_titles))}


async def _generate_gemini(prompt: str, api_key: str, model: str) -> str:
    client = genai.Client(api_key=api_key)
    response = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(system_instruction=_SYSTEM_INSTRUCTION),
    )
    # response.text is Optional — None when the response has no text parts
    # (e.g. blocked by safety filters or an empty candidate).
    return response.text or ""


async def _generate_ollama(prompt: str, ollama_url: str, model: str) -> str:
    """Fully-local generation via Ollama's /api/chat. Failures surface as
    GenerationError so callers can distinguish them from Gemini problems."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_INSTRUCTION},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=_OLLAMA_GEN_TIMEOUT) as client:
            resp = await client.post(f"{ollama_url.rstrip('/')}/api/chat", json=payload)
            resp.raise_for_status()
    except httpx.ConnectError as e:
        raise GenerationError(
            "Cannot reach the local LLM service — Ollama may still be starting up."
        ) from e
    except httpx.TimeoutException as e:
        raise GenerationError(
            "The local LLM timed out — it may be busy or the model too large "
            "for this host. Please try again."
        ) from e
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise GenerationError(
                f"Local LLM model '{model}' is not available — it may still be "
                "downloading. Please wait and try again."
            ) from e
        raise GenerationError(
            f"Local LLM service returned {e.response.status_code}."
        ) from e
    return (resp.json().get("message") or {}).get("content", "")


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
) -> dict:
    """RAG orchestrator: hybrid-rank the candidate tiddlers (see `retrieve`),
    keep the top rag_top_k, and hand budgeted excerpts to the configured
    generation backend (`cfg.llm_backend`: Gemini or local Ollama).

    `max_embed` only bounds how many embedding-cache *misses* are computed this
    request. Skipped misses are cached on later requests, so coverage converges
    to the full corpus. Returns {answer, sources, truncated} — `truncated` is
    True while some chunks were skipped (i.e. the answer may not have seen
    everything yet).
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
    result = await _generate(question, selected, cfg)
    result["truncated"] = truncated
    return result
