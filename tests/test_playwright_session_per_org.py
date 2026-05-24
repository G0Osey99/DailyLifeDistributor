"""playwright_session blob helpers are per-org and write to per-org paths."""
from __future__ import annotations

import os
import pytest

from core import db, secrets_store, playwright_session as pws


@pytest.fixture(autouse=True)
def _iso_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    import importlib
    importlib.reload(db); db.init_db()
    yield tmp_path


def test_load_session_blob_routes_by_org(tmp_path):
    base = "simplecast_session.json"
    secrets_store.set_blob("playwright.simplecast_session", b'{"x":1}', org_id=1)
    secrets_store.set_blob("playwright.simplecast_session", b'{"x":2}', org_id=2)
    dst1 = tmp_path / "org1" / base; dst1.parent.mkdir()
    dst2 = tmp_path / "org2" / base; dst2.parent.mkdir()
    assert pws._load_session_blob_to(str(dst1), org_id=1)
    assert pws._load_session_blob_to(str(dst2), org_id=2)
    assert dst1.read_bytes() == b'{"x":1}'
    assert dst2.read_bytes() == b'{"x":2}'


def test_persist_session_blob_writes_to_target_org(tmp_path):
    f = tmp_path / "simplecast_session.json"
    f.write_bytes(b'{"y":42}')
    pws._persist_session_blob(str(f), org_id=5)
    assert secrets_store.get_blob("playwright.simplecast_session", org_id=5) == b'{"y":42}'
    assert secrets_store.get_blob("playwright.simplecast_session", org_id=6) is None


def test_has_session_checks_target_org(tmp_path):
    f = tmp_path / "simplecast_session.json"
    assert pws.has_session(str(f), org_id=1) is False
    secrets_store.set_blob("playwright.simplecast_session", b"x", org_id=1)
    assert pws.has_session(str(f), org_id=1) is True
    assert pws.has_session(str(f), org_id=2) is False


def test_clear_session_only_removes_target_org(tmp_path):
    f = tmp_path / "simplecast_session.json"
    secrets_store.set_blob("playwright.simplecast_session", b"a", org_id=1)
    secrets_store.set_blob("playwright.simplecast_session", b"b", org_id=2)
    pws.clear_session(str(f), org_id=1)
    assert secrets_store.get_blob("playwright.simplecast_session", org_id=1) is None
    assert secrets_store.get_blob("playwright.simplecast_session", org_id=2) == b"b"
