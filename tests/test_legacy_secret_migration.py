"""migration_bootstrap moves legacy unscoped secrets to the bootstrap org's scope."""
from __future__ import annotations

import pytest

from core import db, secrets_store, migration_bootstrap, org_store, user_store


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SECRET_ENC_KEY", "QcOzn6q5y8yp7v4OoH5sNcWZS_VqIyqU0o8jOwHjW6w=")
    monkeypatch.setenv("PROGRAM_OWNER_EMAIL", "po@x")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "pw1234567")
    import importlib
    import core.db, core.org_store, core.user_store, core.secrets_store
    importlib.reload(core.db); importlib.reload(core.org_store)
    importlib.reload(core.user_store); importlib.reload(core.secrets_store)
    core.db.init_db()


def test_legacy_kv_secret_moves_to_bootstrap_org(env):
    secrets_store.set_secret("UNSPLASH_ACCESS_KEY", "k-legacy")
    migration_bootstrap.run_migration()
    org = org_store.get_org_by_slug("lcbc-church")
    assert secrets_store.get_secret("UNSPLASH_ACCESS_KEY", org_id=org["id"]) == "k-legacy"
    assert secrets_store.get_secret("UNSPLASH_ACCESS_KEY") is None


def test_legacy_blob_moves_to_bootstrap_org(env):
    secrets_store.set_blob("playwright.simplecast_session", b"sess-bytes")
    migration_bootstrap.run_migration()
    org = org_store.get_org_by_slug("lcbc-church")
    assert secrets_store.get_blob("playwright.simplecast_session", org_id=org["id"]) == b"sess-bytes"


def test_client_secrets_moves_to_platform_scope(env):
    secrets_store.set_blob("youtube.client_secrets", b'{"web":{}}')
    migration_bootstrap.run_migration()
    assert secrets_store.get_platform_blob("youtube.client_secrets") == b'{"web":{}}'
    assert secrets_store.get_blob("youtube.client_secrets") is None


def test_password_hash_stays_unscoped(env):
    from core.auth import _HASH_SECRET
    secrets_store.set_secret(_HASH_SECRET, "$argon2id$...")
    migration_bootstrap.run_migration()
    # Legacy slot intact, NOT moved to org scope.
    assert secrets_store.get_secret(_HASH_SECRET) is not None


def test_migration_is_idempotent(env):
    secrets_store.set_secret("UNSPLASH_ACCESS_KEY", "k-legacy")
    migration_bootstrap.run_migration()
    migration_bootstrap.run_migration()  # second time = no-op
    org = org_store.get_org_by_slug("lcbc-church")
    assert secrets_store.get_secret("UNSPLASH_ACCESS_KEY", org_id=org["id"]) == "k-legacy"
