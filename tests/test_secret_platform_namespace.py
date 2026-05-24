"""platform:<name> namespace for cross-tenant shared secrets."""
from __future__ import annotations

import pytest

from core import db, secrets_store


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    import importlib
    importlib.reload(db); db.init_db()
    yield


def test_set_then_get_platform_secret():
    secrets_store.set_platform_secret("foo", "v1")
    assert secrets_store.get_platform_secret("foo") == "v1"


def test_platform_and_org_share_a_name_but_not_storage():
    secrets_store.set_platform_secret("k", "platform-v")
    secrets_store.set_secret("k", "org-v", org_id=1)
    assert secrets_store.get_platform_secret("k") == "platform-v"
    assert secrets_store.get_secret("k", org_id=1) == "org-v"
    assert secrets_store.get_secret("k") is None  # legacy slot empty


def test_set_then_get_platform_blob():
    secrets_store.set_platform_blob("b", b"\x00\x01")
    assert secrets_store.get_platform_blob("b") == b"\x00\x01"


def test_has_and_delete_platform_secret():
    secrets_store.set_platform_secret("x", "y")
    assert secrets_store.has_platform_secret("x") is True
    secrets_store.delete_platform_secret("x")
    assert secrets_store.has_platform_secret("x") is False


def test_list_secret_names_excludes_platform_from_org_scope():
    secrets_store.set_platform_secret("p", "v")
    secrets_store.set_secret("o", "v", org_id=1)
    assert "p" not in secrets_store.list_secret_names(org_id=1)
    assert "p" not in secrets_store.list_secret_names()  # legacy listing
