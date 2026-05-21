"""Unit tests for the encrypted secret store."""
import os

import pytest

from core import secrets_store


@pytest.fixture(autouse=True)
def _db(temp_db):
    yield


def test_kv_round_trip():
    secrets_store.set_secret("api.key", "abc123")
    assert secrets_store.get_secret("api.key") == "abc123"


def test_get_unset_returns_none():
    assert secrets_store.get_secret("does.not.exist") is None


def test_has_secret():
    assert secrets_store.has_secret("x") is False
    secrets_store.set_secret("x", "y")
    assert secrets_store.has_secret("x") is True


def test_overwrite():
    secrets_store.set_secret("k", "v1")
    secrets_store.set_secret("k", "v2")
    assert secrets_store.get_secret("k") == "v2"


def test_delete():
    secrets_store.set_secret("k", "v")
    secrets_store.delete_secret("k")
    assert secrets_store.get_secret("k") is None


def test_list_names():
    secrets_store.set_secret("a", "1")
    secrets_store.set_secret("b", "2")
    assert set(secrets_store.list_secret_names()) == {"a", "b"}


def test_value_is_encrypted_at_rest():
    secrets_store.set_secret("k", "PLAINTEXT_MARKER")
    from core.db import _get_conn
    with _get_conn() as conn:
        row = conn.execute("SELECT value FROM secrets WHERE name='k'").fetchone()
    assert b"PLAINTEXT_MARKER" not in bytes(row["value"])


def test_blob_round_trip():
    secrets_store.set_blob("session", b"\x00\x01binarydata\xff")
    assert secrets_store.get_blob("session") == b"\x00\x01binarydata\xff"


def test_materialize_blob_to_tempfile_creates_then_removes():
    secrets_store.set_blob("file", b"contents")
    seen_path = None
    with secrets_store.materialize_blob_to_tempfile("file") as path:
        seen_path = path
        assert os.path.exists(path)
        with open(path, "rb") as f:
            assert f.read() == b"contents"
    assert not os.path.exists(seen_path)


def test_corrupt_secret_returns_none(monkeypatch):
    secrets_store.set_secret("k", "v")
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SECRET_ENC_KEY", Fernet.generate_key().decode())
    assert secrets_store.get_secret("k") is None
