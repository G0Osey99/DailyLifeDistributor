"""Phase γ Task 21: /settings/audit-log + role gating + org-scoped."""
from __future__ import annotations

from core import audit
from tests.helpers import add_membership, login_as, make_org, make_user


def test_user_role_forbidden(client, db):
    org = make_org(db, "Acme")
    user = make_user(db, username="u")
    add_membership(db, user["id"], org["id"], role="user")
    login_as(client, user, current_org_id=org["id"])
    resp = client.get("/settings/audit-log")
    assert resp.status_code == 403


def test_owner_sees_org_scoped_events_only(client, db):
    org_a = make_org(db, "Acme")
    org_b = make_org(db, "Beta", slug="beta")
    owner = make_user(db, username="o")
    add_membership(db, owner["id"], org_a["id"], role="owner")
    audit.write_event(action="upload.started", actor_user_id=99, org_id=org_a["id"])
    audit.write_event(action="upload.started", actor_user_id=99, org_id=org_b["id"])
    login_as(client, owner, current_org_id=org_a["id"])
    resp = client.get("/settings/audit-log")
    body = resp.get_data(as_text=True)
    # Only one upload.started row visible
    assert body.count("upload.started") == 1


def test_filters_by_action(client, db):
    org = make_org(db, "Acme")
    owner = make_user(db, username="o")
    add_membership(db, owner["id"], org["id"], role="owner")
    audit.write_event(action="user.login", actor_user_id=owner["id"], org_id=org["id"])
    audit.write_event(action="upload.failed", actor_user_id=owner["id"], org_id=org["id"])
    login_as(client, owner, current_org_id=org["id"])
    r = client.get("/settings/audit-log?action=upload.")
    body = r.get_data(as_text=True)
    assert "upload.failed" in body
    assert "user.login" not in body


def test_manager_can_see_audit_log(client, db):
    org = make_org(db, "Acme")
    mgr = make_user(db, username="m")
    add_membership(db, mgr["id"], org["id"], role="manager")
    login_as(client, mgr, current_org_id=org["id"])
    resp = client.get("/settings/audit-log")
    assert resp.status_code == 200
