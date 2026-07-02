"""Config loader: YAML parsing, env fallbacks, per-notebook secrets."""

import pytest

from app.config import load_config

MINIMAL_YAML = """
notebooks:
  - name: ops
    app_url: https://tw.lab.hole/app/
  - name: dev
    app_url: http://tw-dev:8080
    probe_filter: "[!is[system]]"
    unlock:
      password_selector: "#pw"
"""


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_YAML)
    monkeypatch.setenv("CONFIG_PATH", str(path))
    monkeypatch.setenv("GATEWAY_API_KEY", "gw-key")
    # Make sure ambient env from the dev box doesn't leak into assertions.
    for var in (
        "GEMINI_API_KEY", "GEMINI_MODEL", "OLLAMA_URL", "EMBED_MODEL",
        "RAG_TOP_K", "TWPWA_OPS_TOKEN", "TWPWA_OPS_PASSWORD",
        "TWPWA_DEV_TOKEN", "TWPWA_DEV_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_notebooks_and_defaults(config_env):
    cfg = load_config()
    assert cfg.api_key == "gw-key"
    assert list(cfg.notebooks) == ["ops", "dev"]

    ops = cfg.notebooks["ops"]
    assert ops.app_url == "https://tw.lab.hole/app/"
    assert ops.probe_filter == "[tiddlypwa[]]"  # default
    assert ops.unlock.password_selector == 'input[name="password"]'

    dev = cfg.notebooks["dev"]
    assert dev.probe_filter == "[!is[system]]"  # per-notebook override
    assert dev.unlock.password_selector == "#pw"
    # Unset unlock keys keep their defaults even when the block is present.
    assert dev.unlock.login_button == 'button:has-text("Log in")'

    # Env-fallback defaults.
    assert cfg.gemini_api_key == ""
    assert cfg.ollama_url == "http://ollama:11434"
    assert cfg.embed_model == "nomic-embed-text"
    assert cfg.rag_top_k == 8


def test_env_overrides_and_notebook_secrets(config_env):
    config_env.setenv("GEMINI_API_KEY", "gem-key")
    config_env.setenv("GEMINI_MODEL", "gemini-3.5-flash")
    config_env.setenv("RAG_TOP_K", "3")
    config_env.setenv("TWPWA_OPS_TOKEN", "ops-token")
    config_env.setenv("TWPWA_OPS_PASSWORD", "ops-pass")

    cfg = load_config()
    assert cfg.gemini_api_key == "gem-key"
    assert cfg.gemini_model == "gemini-3.5-flash"
    assert cfg.rag_top_k == 3
    # Secrets resolve per notebook by upper-cased name; absent ones stay empty.
    assert cfg.notebooks["ops"].token == "ops-token"
    assert cfg.notebooks["ops"].password == "ops-pass"
    assert cfg.notebooks["dev"].token == ""
    assert cfg.notebooks["dev"].password == ""


def test_missing_gateway_key_raises(config_env):
    config_env.delenv("GATEWAY_API_KEY")
    with pytest.raises(KeyError):
        load_config()
