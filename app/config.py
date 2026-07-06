import os
from dataclasses import dataclass


@dataclass
class AppConfig:
    api_key: str
    gemini_api_key: str
    gemini_model: str
    # Persistent caches (embeddings + note cache) live here — a named volume
    # in Docker so restarts stay warm.
    profiles_dir: str = "/app/profiles"
    # RAG retrieval (local Ollama embeddings). max_tiddlers (per-request) caps the
    # candidate set that gets embedded/ranked; rag_top_k caps how many survive into
    # the generation prompt.
    ollama_url: str = "http://ollama:11434"
    embed_model: str = "nomic-embed-text"
    rag_top_k: int = 8
    # Fold prior chat turns into a standalone retrieval query before ranking
    # (one extra generation call per follow-up). Off → retrieve by the raw
    # question, as before.
    query_rewrite: bool = True
    # Generation backend: "gemini" (default) or "ollama" for fully-local asks.
    llm_backend: str = "gemini"
    ollama_llm_model: str = "llama3.2"


def load_config() -> AppConfig:
    """Env-only configuration. The server holds no notebook credentials —
    all note content arrives in request bodies from the plugin."""
    return AppConfig(
        api_key=os.environ["GATEWAY_API_KEY"],
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite"),
        profiles_dir=os.environ.get("PROFILES_DIR", "/app/profiles"),
        ollama_url=os.environ.get("OLLAMA_URL", "http://ollama:11434"),
        embed_model=os.environ.get("EMBED_MODEL", "nomic-embed-text"),
        rag_top_k=int(os.environ.get("RAG_TOP_K", "8")),
        query_rewrite=os.environ.get("RAG_QUERY_REWRITE", "true").strip().lower()
        not in ("0", "false", "no", "off"),
        llm_backend=os.environ.get("LLM_BACKEND", "gemini"),
        ollama_llm_model=os.environ.get("OLLAMA_LLM_MODEL", "llama3.2"),
    )
