"""Per-org isolation in core.secrets_store.

The phase-β contract is: every accessor takes an optional ``org_id`` kwarg;
the storage name is namespaced under ``org:<id>:<name>``; legacy (org_id=None)
rows stay backward-compatible.
"""
from __future__ import annotations

import pytest

from core import secrets_store


def test_set_get_secret_scoped_by_org():
    secrets_store.set_secret("yt_token", "tok-A", org_id=1)
    secrets_store.set_secret("yt_token", "tok-B", org_id=2)
    assert secrets_store.get_secret("yt_token", org_id=1) == "tok-A"
    assert secrets_store.get_secret("yt_token", org_id=2) == "tok-B"


def test_legacy_null_org_unaffected_by_scoped_writes():
    secrets_store.set_secret("legacy_key", "legacy-val")
    secrets_store.set_secret("legacy_key", "org1-val", org_id=1)
    assert secrets_store.get_secret("legacy_key") == "legacy-val"
    assert secrets_store.get_secret("legacy_key", org_id=1) == "org1-val"


def test_scoped_delete_does_not_touch_legacy():
    secrets_store.set_secret("k", "legacy-v")
    secrets_store.set_secret("k", "org5-v", org_id=5)
    secrets_store.delete_secret("k", org_id=5)
    assert secrets_store.get_secret("k") == "legacy-v"
    assert secrets_store.get_secret("k", org_id=5) is None


def test_has_secret_scoped():
    secrets_store.set_secret("foo", "v", org_id=7)
    assert secrets_store.has_secret("foo", org_id=7) is True
    assert secrets_store.has_secret("foo", org_id=8) is False
    assert secrets_store.has_secret("foo") is False  # legacy slot empty


def test_list_secret_names_isolated_per_scope():
    secrets_store.set_secret("a", "1")
    secrets_store.set_secret("b", "2", org_id=1)
    secrets_store.set_secret("c", "3", org_id=2)
    assert set(secrets_store.list_secret_names()) == {"a"}
    assert set(secrets_store.list_secret_names(org_id=1)) == {"b"}
    assert set(secrets_store.list_secret_names(org_id=2)) == {"c"}


def test_blob_scope_isolated():
    secrets_store.set_blob("session", b"orgA-bytes", org_id=1)
    secrets_store.set_blob("session", b"orgB-bytes", org_id=2)
    assert secrets_store.get_blob("session", org_id=1) == b"orgA-bytes"
    assert secrets_store.get_blob("session", org_id=2) == b"orgB-bytes"


def test_org_id_column_populated():
    """The schema column should record the scope so a future migration to
    a composite primary key can dedupe rows."""
    from core import db
    secrets_store.set_secret("name1", "v", org_id=42)
    with db._get_conn() as c:
        row = c.execute(
            "SELECT org_id FROM secrets WHERE name='org:42:name1'"
        ).fetchone()
    assert row["org_id"] == 42
