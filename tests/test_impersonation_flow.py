"""Program-owner impersonation: enter, exit, audit, role-gate."""
from __future__ import annotations

import pytest

from core import db, user_store, org_store


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
    return m.app


def _po(app):
    po_org = org_store.create_org(name="Bootstrap", slug="boot")
    po = user_store.create_user(
        username="po", email="po@x", password="pw1234567", program_owner=True,
    )
    org_store.add_membership(user_id=po["id"], org_id=po_org["id"], role="owner")
    target = org_store.create_org(name="Target", slug="target")
    return po, po_org, target


def _login(client, uid, org_id):
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["current_org_id"] = org_id
        sess["permission_2fa_passed"] = True


def test_owner_can_enter_and_exit_impersonation(app):
    po, po_org, target = _po(app)
    client = app.test_client()
    _login(client, po["id"], po_org["id"])
    res = client.post(f"/admin/organizations/{target['id']}/impersonate",
                      follow_redirects=False)
    assert res.status_code == 302
    with client.session_transaction() as s:
        assert s.get("acting_as_org_id") == target["id"]
    client.post("/admin/exit-impersonation", follow_redirects=False)
    with client.session_transaction() as s:
        assert s.get("acting_as_org_id") is None


def test_non_owner_cannot_enter_impersonation(app):
    po, po_org, target = _po(app)
    user = user_store.create_user(
        username="u", email="u@x", password="pw1234567", program_owner=False,
    )
    org_store.add_membership(user_id=user["id"], org_id=po_org["id"], role="owner")
    client = app.test_client()
    _login(client, user["id"], po_org["id"])
    res = client.post(f"/admin/organizations/{target['id']}/impersonate")
    assert res.status_code == 403


def test_impersonation_writes_audit_events(app):
    po, po_org, target = _po(app)
    client = app.test_client()
    _login(client, po["id"], po_org["id"])
    client.post(f"/admin/organizations/{target['id']}/impersonate")
    client.post("/admin/exit-impersonation")
    with db._get_conn() as c:
        rows = c.execute(
            "SELECT action, actor_user_id, acting_as_org_id "
            "FROM audit_log ORDER BY id"
        ).fetchall()
    actions = [r["action"] for r in rows]
    assert "impersonation.start" in actions
    assert "impersonation.end" in actions
    starts = [r for r in rows if r["action"] == "impersonation.start"]
    assert starts and starts[0]["actor_user_id"] == po["id"]
    assert starts[0]["acting_as_org_id"] == target["id"]


def test_banner_appears_in_response_while_impersonating(app):
    """After entering impersonation, every page response should contain the
    'Acting as' banner string injected by the context processor + base template."""
    po, po_org, target = _po(app)
    client = app.test_client()
    _login(client, po["id"], po_org["id"])
    client.post(f"/admin/organizations/{target['id']}/impersonate",
                follow_redirects=False)
    # GET any auth-gated page that extends base.html; /history is a stable choice.
    res = client.get("/history", follow_redirects=True)
    assert b"Acting as" in res.data
