"""Playwright session blobs round-trip through the encrypted store."""
import os

import pytest

from core import playwright_session as ps
from core import secrets_store


@pytest.fixture(autouse=True)
def _db(temp_db):
    yield


def test_secret_name_per_service():
    assert ps._session_secret_name("/x/simplecast_session.json") == "playwright.simplecast_session"
    assert ps._session_secret_name("/x/rock_session.json") == "playwright.rock_session"


def test_persist_then_load(tmp_path):
    session_file = str(tmp_path / "rock_session.json")
    with open(session_file, "w") as f:
        f.write('{"cookies": []}')
    ps._persist_session_blob(session_file)
    os.remove(session_file)
    assert ps._load_session_blob_to(session_file) is True
    with open(session_file) as f:
        assert f.read() == '{"cookies": []}'


def test_load_missing_returns_false(tmp_path):
    session_file = str(tmp_path / "absent_session.json")
    assert ps._load_session_blob_to(session_file) is False


def test_session_encrypted_at_rest(tmp_path):
    session_file = str(tmp_path / "vista_social_session.json")
    with open(session_file, "w") as f:
        f.write("COOKIE_SECRET")
    ps._persist_session_blob(session_file)
    from core.db import _get_conn
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM secrets WHERE name=?",
            (ps._session_secret_name(session_file),),
        ).fetchone()
    assert b"COOKIE_SECRET" not in bytes(row["value"])


def test_has_and_clear_session(tmp_path):
    session_file = str(tmp_path / "rock_session.json")
    assert ps.has_session(session_file) is False
    with open(session_file, "w") as f:
        f.write("{}")
    ps._persist_session_blob(session_file)
    assert ps.has_session(session_file) is True
    ps.clear_session(session_file)
    assert ps.has_session(session_file) is False
    assert not os.path.exists(session_file)
