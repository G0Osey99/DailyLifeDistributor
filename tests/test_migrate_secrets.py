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


def test_shreds_plaintext_after_import(monkeypatch, tmp_path):
    """The plaintext credential file must not survive the import."""
    monkeypatch.setattr(migrate_secrets, "PROJECT_ROOT", str(tmp_path))
    token = tmp_path / "token.json"
    token.write_text('{"refresh_token": "r"}')
    migrate_secrets.run()
    assert not token.exists()  # plaintext removed; only the encrypted copy remains
    assert secrets_store.get_secret("youtube.token") == '{"refresh_token": "r"}'


def test_shreds_lingering_plaintext_even_when_already_stored(monkeypatch, tmp_path):
    """A leftover plaintext copy is removed even on an idempotent re-run."""
    monkeypatch.setattr(migrate_secrets, "PROJECT_ROOT", str(tmp_path))
    secrets_store.set_secret("youtube.token", '{"refresh_token": "already"}')
    token = tmp_path / "token.json"
    token.write_text('{"refresh_token": "stale-plaintext"}')
    migrate_secrets.run()
    assert not token.exists()


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
