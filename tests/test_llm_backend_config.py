"""The title-suggestion LLM endpoint/model are configurable via env.

Defaults match the bundled llamafile; overriding lets the same code talk to
any OpenAI-compatible backend (e.g. Ollama on a VPS).
"""
import importlib

import pytest


@pytest.fixture(autouse=True)
def _restore_module():
    # The tests reload core.llm_title_gen with different env; reload once more
    # with the test's env cleared on teardown so defaults don't leak to other
    # test files that import the module.
    yield
    import core.llm_title_gen as m
    importlib.reload(m)


def test_defaults_to_llamafile(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    import core.llm_title_gen as m
    importlib.reload(m)
    assert m.LLM_BASE_URL == "http://localhost:8081"
    assert m.LLM_MODEL == "local"
    # /health and older imports rely on this alias tracking the configured URL.
    assert m.LLAMAFILE_BASE_URL == m.LLM_BASE_URL


def test_env_override_for_ollama(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/")
    monkeypatch.setenv("LLM_MODEL", "llama3.2")
    import core.llm_title_gen as m
    importlib.reload(m)
    assert m.LLM_BASE_URL == "http://localhost:11434"  # trailing slash stripped
    assert m.LLM_MODEL == "llama3.2"
    assert m.LLAMAFILE_BASE_URL == "http://localhost:11434"
