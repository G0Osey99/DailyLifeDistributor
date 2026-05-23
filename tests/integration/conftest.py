"""Integration-test fixture overrides.

The top-level ``tests/conftest.py`` redirects every state.db read/write to a
per-test temp file (autouse) so unit tests can't pollute the developer's real
DB. The ``*_live.py`` tests in this directory are the opposite — they want
to read the real (encrypted) secrets the user has paired/authenticated and
hit the live service. Without an override they:

  1. skipif() evaluates the real DB at collection time, sees secrets present,
     and lets the test run; then
  2. the autouse fixture blanks state.db to a fresh tmp file, so the test
     body's ``secrets_store.get_*`` call returns None and the source raises
     FileNotFoundError / SessionExpiredError.

This conftest neutralises the isolation specifically for files matching
``*_live.py`` so the live source tests see the real secrets the skipif gate
already verified are present. Other integration tests (relay/e2e/agent) keep
the isolation — they don't read real secrets.
"""
from __future__ import annotations

import os

import pytest

# Live tests read SECRET_ENC_KEY (and other knobs) from the project ``.env``
# the same way ``app.py`` does at boot. Pytest itself doesn't load ``.env``,
# so we do it here once at collection time — before any fixture decides
# whether to override the master key for a live test.
try:
    from dotenv import load_dotenv

    _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")
    if os.path.isfile(_ENV_PATH):
        load_dotenv(_ENV_PATH)
except Exception:  # pragma: no cover - python-dotenv missing in pure-CI envs
    pass


def _is_live_file(request: pytest.FixtureRequest) -> bool:
    fspath = getattr(request.node, "fspath", None) or ""
    name = str(fspath)
    return name.endswith("_live.py")


@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch, request):
    """Override the parent autouse to skip isolation for ``*_live.py``.

    For non-live integration tests we replicate the parent fixture's
    behaviour (redirect + init schema) so they still get a clean DB. For
    live tests we leave the real ``_DB_PATH`` in place so the encrypted
    secrets the user has paired are readable.
    """
    if _is_live_file(request):
        # Real DB / real secrets. The skipif gate at the top of each
        # ``*_live.py`` already filtered out cases where the secret is
        # absent, so the test body can rely on ``get_secret`` returning a
        # real value.
        yield
        return
    db_path = str(tmp_path / "isolated_state.db")
    from core import db as _db
    from core import quota as _quota
    monkeypatch.setattr(_db, "_DB_PATH", db_path)
    monkeypatch.setattr(_quota, "_DB_PATH", db_path)
    monkeypatch.setattr(_db, "_PRAGMAS_APPLIED", False, raising=False)
    _db.init_db()
    yield


@pytest.fixture(autouse=True)
def _master_key(monkeypatch, request):
    """Override the parent autouse to use the user's real SECRET_ENC_KEY
    for ``*_live.py``.

    Live tests read encrypted blobs the user already wrote with their real
    key; if we override to a freshly-generated Fernet key in this scope, the
    decrypt step fails and the test misreports as "no secret" or raises a
    crypto error. For everything else, mirror the parent behaviour by
    setting a fresh deterministic test key.
    """
    if _is_live_file(request):
        # Trust whatever the operator has in their .env / shell. If it's
        # missing the source will fail loudly on the first ``crypto.decrypt``
        # call, which is the right signal.
        yield
        return
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SECRET_ENC_KEY", Fernet.generate_key().decode())
    yield
