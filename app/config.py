import os
import yaml
from dataclasses import dataclass, field

# What "changed recently" means for scheduled digests: user tiddlers touched in
# the last week, newest first, excluding AI output (earlier digests, saved chat
# notes and their turns) so generated text doesn't feed itself.
DIGEST_DEFAULT_FILTER = (
    "[!is[system]!tag[ai-digest]!tag[ai-chat]!tag[ai-chat-turn]"
    "has[text]days:modified[-7]sort[-modified]limit[20]]"
)


@dataclass
class UnlockConfig:
    password_selector: str = 'input[name="password"]'
    token_selector: str = 'input[name="username"]'
    login_button: str = 'button:has-text("Log in")'


@dataclass
class NotebookConfig:
    name: str
    app_url: str
    unlock: UnlockConfig
    probe_filter: str
    password: str = ""
    token: str = ""


@dataclass
class AppConfig:
    api_key: str
    gemini_api_key: str
    gemini_model: str
    notebooks: dict  # name -> NotebookConfig
    profiles_dir: str = "/app/profiles"
    # RAG retrieval (local Ollama embeddings). max_tiddlers (per-request) caps the
    # candidate set that gets embedded/ranked; rag_top_k caps how many survive into
    # the generation prompt.
    ollama_url: str = "http://ollama:11434"
    embed_model: str = "nomic-embed-text"
    rag_top_k: int = 8
    # Generation backend: "gemini" (default) or "ollama" for fully-local asks.
    llm_backend: str = "gemini"
    ollama_llm_model: str = "llama3.2"
    # Scheduled synthesis digest: daily "what changed" tiddler written into
    # `digest_notebook` at `digest_hour_utc`. Empty notebook = scheduler off
    # (the POST /notebooks/{nb}/digest route works regardless).
    digest_notebook: str = ""
    digest_hour_utc: int = 6
    digest_filter: str = DIGEST_DEFAULT_FILTER


def load_config() -> AppConfig:
    config_path = os.environ.get("CONFIG_PATH", "config.yaml")
    with open(config_path) as f:
        data = yaml.safe_load(f)

    notebooks: dict[str, NotebookConfig] = {}
    for nb in data.get("notebooks", []):
        name = nb["name"]
        unlock_data = nb.get("unlock", {})
        unlock = UnlockConfig(
            password_selector=unlock_data.get("password_selector", 'input[name="password"]'),
            token_selector=unlock_data.get("token_selector", 'input[name="username"]'),
            login_button=unlock_data.get("login_button", 'button:has-text("Log in")'),
        )
        password = os.environ.get(f"TWPWA_{name.upper()}_PASSWORD", "")
        token = os.environ.get(f"TWPWA_{name.upper()}_TOKEN", "")
        notebooks[name] = NotebookConfig(
            name=name,
            app_url=nb["app_url"],
            unlock=unlock,
            probe_filter=nb.get("probe_filter", "[tiddlypwa[]]"),
            password=password,
            token=token,
        )

    return AppConfig(
        api_key=os.environ["GATEWAY_API_KEY"],
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite"),
        notebooks=notebooks,
        profiles_dir=data.get("profiles_dir", "/app/profiles"),
        ollama_url=os.environ.get("OLLAMA_URL", "http://ollama:11434"),
        embed_model=os.environ.get("EMBED_MODEL", "nomic-embed-text"),
        rag_top_k=int(os.environ.get("RAG_TOP_K", "8")),
        llm_backend=os.environ.get("LLM_BACKEND", "gemini"),
        ollama_llm_model=os.environ.get("OLLAMA_LLM_MODEL", "llama3.2"),
        digest_notebook=os.environ.get("DIGEST_NOTEBOOK", ""),
        digest_hour_utc=int(os.environ.get("DIGEST_HOUR_UTC", "6")),
        digest_filter=os.environ.get("DIGEST_FILTER", DIGEST_DEFAULT_FILTER),
    )
