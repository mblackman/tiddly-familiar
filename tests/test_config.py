"""Config loader: env-only settings, defaults, and overrides."""

import pytest

from app.config import load_config


@pytest.fixture
def config_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "gw-key")
    # Make sure ambient env from the dev box doesn't leak into assertions.
    for var in (
        "GEMINI_API_KEY", "GEMINI_MODEL", "PROFILES_DIR", "OLLAMA_URL",
        "EMBED_MODEL", "RAG_TOP_K", "LLM_BACKEND", "OLLAMA_LLM_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_defaults(config_env):
    cfg = load_config()
    assert cfg.api_key == "gw-key"
    assert cfg.gemini_api_key == ""
    assert cfg.profiles_dir == "/app/profiles"
    assert cfg.ollama_url == "http://ollama:11434"
    assert cfg.embed_model == "nomic-embed-text"
    assert cfg.rag_top_k == 8
    assert cfg.llm_backend == "gemini"
    assert cfg.ollama_llm_model == "llama3.2"


def test_env_overrides(config_env):
    config_env.setenv("GEMINI_API_KEY", "gem-key")
    config_env.setenv("GEMINI_MODEL", "gemini-3.5-flash")
    config_env.setenv("PROFILES_DIR", "/tmp/profiles")
    config_env.setenv("RAG_TOP_K", "3")
    config_env.setenv("LLM_BACKEND", "ollama")
    config_env.setenv("OLLAMA_LLM_MODEL", "qwen2.5:0.5b")

    cfg = load_config()
    assert cfg.gemini_api_key == "gem-key"
    assert cfg.gemini_model == "gemini-3.5-flash"
    assert cfg.profiles_dir == "/tmp/profiles"
    assert cfg.rag_top_k == 3
    assert cfg.llm_backend == "ollama"
    assert cfg.ollama_llm_model == "qwen2.5:0.5b"


def test_missing_gateway_key_raises(config_env):
    config_env.delenv("GATEWAY_API_KEY")
    with pytest.raises(KeyError):
        load_config()
