import re

from google import genai
from google.genai import types

from .embeddings import Embedder, rank


def _build_context(tiddlers: list[dict]) -> tuple[str, list[str]]:
    parts = []
    sources = []
    for t in tiddlers:
        title = t.get("title") or t.get("fields", {}).get("title", "")
        text = t.get("text", "").strip()
        if text:
            parts.append(f"## {title}\n{text}")
            sources.append(title)
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


def _tiddler_text(t: dict) -> str:
    """Embeddable representation of a tiddler: title carries signal, so prepend it."""
    title = t.get("title") or t.get("fields", {}).get("title", "")
    return f"{title}\n{t.get('text', '')}".strip()


async def answer_question(
    question: str,
    tiddlers: list[dict],
    embedder: Embedder,
    top_k: int,
    api_key: str,
    model: str,
    max_embed: int | None = None,
) -> dict:
    """RAG orchestrator: embed + cosine-rank the candidate tiddlers, keep the top-k,
    and hand only those to Gemini for generation.

    Every candidate participates in ranking; `max_embed` only bounds how many
    embedding-cache *misses* are computed this request. Skipped misses are cached
    on later requests, so coverage converges to the full corpus. Returns
    {answer, sources, truncated} — `truncated` is True while some candidates
    were skipped (i.e. the answer may not have seen everything yet).
    """
    # Only tiddlers with real text are worth embedding/ranking.
    candidates = [t for t in tiddlers if t.get("text", "").strip()]

    if not candidates:
        return {
            "answer": "There are no notes matching that filter to answer from.",
            "sources": [],
            "truncated": False,
        }

    query_vec = await embedder.embed_query(question)
    doc_vecs = await embedder.embed_documents(
        [_tiddler_text(t) for t in candidates], max_new=max_embed
    )
    embedded = [(i, v) for i, v in enumerate(doc_vecs) if v is not None]
    truncated = len(embedded) < len(candidates)
    ranked = rank(query_vec, [v for _, v in embedded])

    top = [candidates[embedded[i][0]] for i, _score in ranked[:top_k]]
    result = await _generate(question, top, api_key=api_key, model=model)
    result["truncated"] = truncated
    return result
