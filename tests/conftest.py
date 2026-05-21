"""Shared pytest fixtures."""
import os
import pytest


@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch):
    """Redirect every state.db reference to a per-test temp file.

    Without this, any test that touches `core.db`, `core.quota`, or the
    Flask app indirectly writes to the developer's real `state.db` —
    polluting upload_history with fake rows and the YouTube quota
    counter with nonsense. Autouse + project-wide so even tests that
    forget to request `temp_db` still see isolation.
    """
    db_path = str(tmp_path / "isolated_state.db")
    from core import db as _db
    from core import quota as _quota
    monkeypatch.setattr(_db, "_DB_PATH", db_path)
    monkeypatch.setattr(_quota, "_DB_PATH", db_path)
    # Reset the one-shot PRAGMA flag so init_db() re-applies WAL on the
    # new file rather than skipping it.
    monkeypatch.setattr(_db, "_PRAGMAS_APPLIED", False, raising=False)
    yield


@pytest.fixture
def temp_db(_isolate_state_db):
    """Initialised core.db backed by the autouse temp file.

    Kept for tests that explicitly want the module handle (rather than
    relying on the autouse redirection alone).
    """
    from core import db as _db
    _db.init_db()
    yield _db


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    """Provide a valid Fernet master key for every test.

    Crypto/secret-store code fails closed without SECRET_ENC_KEY; set a
    fixed test key so unit tests can encrypt/decrypt deterministically.
    """
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SECRET_ENC_KEY", Fernet.generate_key().decode())
    yield
