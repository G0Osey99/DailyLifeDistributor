"""The plaintext importer is correct and idempotent."""
import pytest

from core import secrets_store
from scripts import migrate_secrets


@pytest.fixture(autouse=True)
def _db(temp_db):
    yield


def test_imports_env_key(monkeypatch):
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "u-key")
    imported = migrate_secrets.run()
    assert "UNSPLASH_ACCESS_KEY" in imported
    assert secrets_store.get_secret("UNSPLASH_ACCESS_KEY") == "u-key"


def test_imports_token_file(monkeypatch, tmp_path):
    monkeypatch.setattr(migrate_secrets, "PROJECT_ROOT", str(tmp_path))
    (tmp_path / "token.json").write_text('{"refresh_token": "r"}')
    imported = migrate_secrets.run()
    assert "youtube.token" in imported
    assert secrets_store.get_secret("youtube.token") == '{"refresh_token": "r"}'


def test_idempotent(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "p-key")
    first = migrate_secrets.run()
    second = migrate_secrets.run()
    assert "PEXELS_API_KEY" in first
    assert "PEXELS_API_KEY" not in second


def test_does_not_overwrite_existing(monkeypatch):
    secrets_store.set_secret("UNSPLASH_ACCESS_KEY", "store-value")
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "env-value")
    migrate_secrets.run()
    assert secrets_store.get_secret("UNSPLASH_ACCESS_KEY") == "store-value"
