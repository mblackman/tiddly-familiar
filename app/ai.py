import re

from google import genai
from google.genai import types

from .embeddings import Embedder, rank

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

    # (candidate index, offset, chunk) for every chunk of every candidate.
    records = [
        (ci, offset, chunk)
        for ci, t in enumerate(candidates)
        for offset, chunk in _chunk_text(t.get("text", ""))
    ]
    embed_texts = [_embed_text(_title(candidates[ci]), chunk) for ci, _o, chunk in records]

    query_vec = await embedder.embed_query(question)
    vecs = await embedder.embed_documents(embed_texts, max_new=max_embed)
    truncated = any(v is None for v in vecs)

    cos = [0.0] * len(records)
    embedded = [(i, v) for i, v in enumerate(vecs) if v is not None]
    for rank_idx, score in rank(query_vec, [v for _i, v in embedded]):
        cos[embedded[rank_idx][0]] = score

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


async def _generate(question: str, tiddlers: list[dict], api_key: str, model: str) -> dict:
    """Send the (already-ranked) tiddlers to Gemini and return {answer, sources}.
    Sources are derived from [[Note Title]] citations in the answer text."""
    context, available_titles = _build_context(tiddlers)
    title_set = set(available_titles)

    client = genai.Client(api_key=api_key)
    prompt = f"Here are the relevant notes:\n\n{context}\n\nQuestion: {question}"

    response = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=(
                "You are answering questions about a personal TiddlyWiki notebook. "
                "Use only the provided notes to answer. "
                "Format the answer in Markdown. "
                "When you reference a specific note, cite it inline using TiddlyWiki link "
                "syntax: [[Note Title]] — use the exact title as given in the notes. "
                "If the notes don't contain enough information, say so."
            ),
        ),
    )

    # response.text is Optional — None when the response has no text parts
    # (e.g. blocked by safety filters or an empty candidate).
    answer = response.text or "The AI model returned no answer for this question."
    return {"answer": answer, "sources": _extract_citations(answer, title_set)}


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
    top_k: int,
    api_key: str,
    model: str,
    max_embed: int | None = None,
) -> dict:
    """RAG orchestrator: hybrid-rank the candidate tiddlers (see `retrieve`),
    keep the top-k, and hand budgeted excerpts to Gemini for generation.

    `max_embed` only bounds how many embedding-cache *misses* are computed this
    request. Skipped misses are cached on later requests, so coverage converges
    to the full corpus. Returns {answer, sources, truncated} — `truncated` is
    True while some chunks were skipped (i.e. the answer may not have seen
    everything yet).
    """
    selected, truncated = await retrieve(
        question, tiddlers, embedder, top_k, max_embed=max_embed
    )
    if not selected:
        return {
            "answer": "There are no notes matching that filter to answer from.",
            "sources": [],
            "truncated": False,
        }
    result = await _generate(question, selected, api_key=api_key, model=model)
    result["truncated"] = truncated
    return result
