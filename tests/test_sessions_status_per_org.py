"""The sidebar /sessions/status endpoint reflects the effective org.

Before this fix, the endpoint called has_session(path) with no org_id, so
every Playwright session dot read from the legacy unscoped slot — empty
post-migration. After Phase 3.2 sessions live under org:<id>:..., so the
status must thread effective_org_id() through.

The YouTube cache is also keyed per effective-org so impersonation doesn't
hand back the previous org's cached value.
"""
from __future__ import annotations

import pytest

from core import db, org_store, secrets_store, user_store


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SECRET_ENC_KEY", "QcOzn6q5y8yp7v4OoH5sNcWZS_VqIyqU0o8jOwHjW6w=")
    monkeypatch.setenv("FLASK_SECRET_KEY", "t")
    import importlib
    import core.db, core.org_store, core.user_store, core.secrets_store
    importlib.reload(core.db); importlib.reload(core.org_store)
    importlib.reload(core.user_store); importlib.reload(core.secrets_store)
    core.db.init_db()
    import app as m; importlib.reload(m)
    # Reset the YT cache between fixtures so values from a sibling test
    # don't leak across.
    m._YT_AUTH_CACHE.clear()
    return m.app


def _login_as(client, user_id, org_id, *, acting_as_org_id=None):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["current_org_id"] = org_id
        sess["permission_2fa_passed"] = True
        if acting_as_org_id is not None:
            sess["acting_as_org_id"] = acting_as_org_id


def test_status_simplecast_reads_effective_org(app):
    org = org_store.create_org(name="A", slug="a")
    user = user_store.create_user(
        username="u", email="u@x", password="pw1234567", program_owner=False,
    )
    org_store.add_membership(user_id=user["id"], org_id=org["id"], role="owner")
    # SimpleCast session blob stored in this org's scope.
    secrets_store.set_blob("playwright.simplecast_session", b"{}", org_id=org["id"])
    client = app.test_client()
    _login_as(client, user["id"], org["id"])
    res = client.get("/sessions/status", headers={"Accept": "application/json"})
    assert res.status_code == 200
    body = res.get_json()
    assert body["simplecast"]["ok"] is True
    assert body["rock"]["ok"] is False  # not stored — must NOT report ok


def test_status_legacy_slot_does_not_leak_into_org_view(app):
    """A stale legacy unscoped session blob must NOT make any org's
    sidebar show green. This pins the bug we just fixed: previously,
    has_session(path) without org_id checked the unscoped row."""
    secrets_store.set_blob("playwright.rock_session", b"{}")  # legacy slot
    org = org_store.create_org(name="A", slug="a")
    user = user_store.create_user(
        username="u", email="u@x", password="pw1234567",
    )
    org_store.add_membership(user_id=user["id"], org_id=org["id"], role="owner")
    client = app.test_client()
    _login_as(client, user["id"], org["id"])
    body = client.get("/sessions/status").get_json()
    assert body["rock"]["ok"] is False


def test_status_swaps_under_impersonation(app):
    po_org = org_store.create_org(name="LCBC", slug="lcbc")
    target = org_store.create_org(name="Tgt", slug="tgt")
    po = user_store.create_user(
        username="po", email="po@x", password="pw1234567", program_owner=True,
    )
    org_store.add_membership(user_id=po["id"], org_id=po_org["id"], role="owner")
    # Target org has the SimpleCast session; bootstrap org does not.
    secrets_store.set_blob("playwright.simplecast_session", b"{}", org_id=target["id"])
    client = app.test_client()
    # As bootstrap: simplecast should be false.
    _login_as(client, po["id"], po_org["id"])
    assert client.get("/sessions/status").get_json()["simplecast"]["ok"] is False
    # Start acting-as the target; simplecast should now be true.
    _login_as(client, po["id"], po_org["id"], acting_as_org_id=target["id"])
    assert client.get("/sessions/status").get_json()["simplecast"]["ok"] is True


def test_yt_cache_is_keyed_by_effective_org(app, monkeypatch):
    """The navbar's YT-auth cache used to be process-global, leaking the
    previous org's truth value across impersonation. Confirm it now
    diverges per effective_org_id.

    Implementation detail: we monkeypatch the underlying yt_is_authenticated
    to switch on the effective org, rather than constructing realistic
    OAuth credentials in the secret store. The cache layer is what we're
    pinning here, not the credential parser.
    """
    import app as m
    from core.org_context import effective_org_id
    org_a = org_store.create_org(name="A", slug="a")
    org_b = org_store.create_org(name="B", slug="b")
    user = user_store.create_user(
        username="u", email="u@x", password="pw1234567",
    )
    org_store.add_membership(user_id=user["id"], org_id=org_a["id"], role="owner")
    org_store.add_membership(user_id=user["id"], org_id=org_b["id"], role="owner")

    # Org A: authed. Org B: not. Anything else: shouldn't be queried.
    def fake_is_authed():
        return effective_org_id() == org_a["id"]
    monkeypatch.setattr(m, "yt_is_authenticated", fake_is_authed)
    m._YT_AUTH_CACHE.clear()

    client = app.test_client()
    _login_as(client, user["id"], org_a["id"])
    body_a = client.get("/sessions/status").get_json()
    _login_as(client, user["id"], org_b["id"])
    body_b = client.get("/sessions/status").get_json()
    assert body_a["youtube"]["ok"] is True, "org A should report YT authed"
    assert body_b["youtube"]["ok"] is False, (
        "org B should report YT NOT authed — got the cached value from A"
    )
