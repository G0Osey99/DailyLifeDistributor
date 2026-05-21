"""API-key resolution prefers the store, falls back to env."""
import pytest

from core import image_gatherer, secrets_store


@pytest.fixture(autouse=True)
def _db(temp_db):
    yield


def test_store_value_preferred(monkeypatch):
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "from-env")
    secrets_store.set_secret("UNSPLASH_ACCESS_KEY", "from-store")
    assert image_gatherer._resolve_key("UNSPLASH_ACCESS_KEY") == "from-store"


def test_env_fallback(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "env-only")
    assert image_gatherer._resolve_key("PEXELS_API_KEY") == "env-only"


def test_unset_returns_empty(monkeypatch):
    monkeypatch.delenv("UNSPLASH_ACCESS_KEY", raising=False)
    assert image_gatherer._resolve_key("UNSPLASH_ACCESS_KEY") == ""
