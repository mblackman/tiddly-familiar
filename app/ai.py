import asyncio
import google.generativeai as genai


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


async def ask_gemini(
    question: str,
    tiddlers: list[dict],
    api_key: str,
    model: str,
) -> dict:
    context, sources = _build_context(tiddlers)

    genai.configure(api_key=api_key)
    gemini = genai.GenerativeModel(
        model,
        system_instruction=(
            "You are answering questions about a personal TiddlyWiki notebook. "
            "Use only the provided notes to answer. "
            "If the notes don't contain enough information, say so."
        ),
    )

    prompt = f"Here are the relevant notes:\n\n{context}\n\nQuestion: {question}"
    response = await asyncio.to_thread(gemini.generate_content, prompt)

    return {"answer": response.text, "sources": sources}
