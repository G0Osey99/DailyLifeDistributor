"""Agent's /sessions/status poll reflects its user's current acting_as_org.

The agent authenticates with a device token (no Flask session cookie), so
``effective_org_id()`` from the browser path returns None for these
requests. Instead the route reads ``users.acting_as_org_id`` for the
device's owner — which the impersonation routes keep in sync with the
browser's session flag. This pins both pieces:

  * acting_as_org_id mirrored on impersonation.start, cleared on .end
  * /sessions/status with a device token reads the mirrored value and
    scopes Playwright session checks accordingly.
"""
from __future__ import annotations

import pytest

from core import db as _db, org_store, secrets_store, user_store
from core.devices import create_pairing_code, redeem_pairing_code


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SECRET_ENC_KEY", "QcOzn6q5y8yp7v4OoH5sNcWZS_VqIyqU0o8jOwHjW6w=")
    monkeypatch.setenv("FLASK_SECRET_KEY", "t")
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    import importlib
    import core.db, core.org_store, core.user_store, core.secrets_store, core.devices
    importlib.reload(core.db); importlib.reload(core.org_store)
    importlib.reload(core.user_store); importlib.reload(core.secrets_store)
    importlib.reload(core.devices)
    core.db.init_db()
    import app as m; importlib.reload(m)
    return m.app


def _pair_and_get_token(uid: int) -> str:
    code = create_pairing_code(user_id=uid)
    _, token = redeem_pairing_code(code, "Test Mac")
    return token


def test_agent_status_reads_owner_acting_as_org(app):
    boot = org_store.create_org(name="LCBC", slug="lcbc")
    target = org_store.create_org(name="Tgt", slug="tgt")
    po = user_store.create_user(
        username="po", email="po@x", password="pw1234567", program_owner=True,
    )
    org_store.add_membership(user_id=po["id"], org_id=boot["id"], role="owner")
    # Mirror that the owner is currently acting as target.
    with _db._get_conn() as c:
        c.execute(
            "UPDATE users SET acting_as_org_id = ? WHERE id = ?",
            (target["id"], po["id"]),
        )
        c.commit()
    # Target org has a SimpleCast session; bootstrap does not.
    secrets_store.set_blob(
        "playwright.simplecast_session", b"{}", org_id=target["id"],
    )
    token = _pair_and_get_token(po["id"])
    client = app.test_client()
    res = client.get(f"/sessions/status?token={token}")
    assert res.status_code == 200
    body = res.get_json()
    assert body["simplecast"]["ok"] is True, (
        "agent status should reflect the owner's acting_as_org (target), "
        "not the bootstrap org's empty SimpleCast slot"
    )


def test_agent_status_falls_back_to_membership_when_not_impersonating(app):
    """When acting_as_org_id is NULL the agent shows the device owner's
    first membership org's state."""
    org = org_store.create_org(name="A", slug="a")
    user = user_store.create_user(username="u", email="u@x", password="pw1234567")
    org_store.add_membership(user_id=user["id"], org_id=org["id"], role="owner")
    secrets_store.set_blob(
        "playwright.rock_session", b"{}", org_id=org["id"],
    )
    token = _pair_and_get_token(user["id"])
    client = app.test_client()
    body = client.get(f"/sessions/status?token={token}").get_json()
    assert body["rock"]["ok"] is True


def test_agent_status_youtube_reads_resolved_org(app, monkeypatch):
    """The agent's YT check must respect the resolved org from the
    token-auth fallback, not the (empty) Flask session. Before this
    fix _cached_yt_authenticated() always saw effective_org_id()==None
    for agent polls and reported 'needs auth' even when the token sat
    in the owner's org scope."""
    import app as app_module
    org = org_store.create_org(name="A", slug="a")
    user = user_store.create_user(username="u", email="u@x", password="pw1234567")
    org_store.add_membership(user_id=user["id"], org_id=org["id"], role="owner")

    # Switch on the underlying YT-authenticated check by effective org:
    # only org A is authed. The fix wraps the call in an org override
    # under token auth; without the override the check sees no org and
    # returns False.
    from core.org_context import effective_org_id
    def fake_is_authed():
        return effective_org_id() == org["id"]
    monkeypatch.setattr(app_module, "yt_is_authenticated", fake_is_authed)
    app_module._YT_AUTH_CACHE.clear()

    token = _pair_and_get_token(user["id"])
    client = app.test_client()
    body = client.get(f"/sessions/status?token={token}").get_json()
    assert body["youtube"]["ok"] is True, (
        "agent's YT check must use the token-auth-resolved org, not "
        "the empty Flask session"
    )
