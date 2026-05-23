"""Phase γ Task 20: audit hooks fire from privileged actions."""
from __future__ import annotations

from tests.helpers import add_membership, login_as, make_org, make_user


def test_login_writes_audit_event(client, db):
    make_user(db, username="alice", password="hunter22hunter22")
    client.post(
        "/login",
        data={"username": "alice", "password": "hunter22hunter22"},
        environ_base={"REMOTE_ADDR": "9.9.9.9", "HTTP_USER_AGENT": "TestUA"},
    )
    rows = db.list_audit_events()
    assert any(
        r["action"] == "user.login" and r["ip"] == "9.9.9.9" and r["user_agent"] == "TestUA"
        for r in rows
    )


def test_failed_login_writes_audit_event(client, db):
    make_user(db, username="alice", password="hunter22hunter22")
    client.post("/login", data={"username": "alice", "password": "wrong"})
    assert any(
        r["action"] == "user.login_failed" for r in db.list_audit_events()
    )


def test_2fa_setup_started_writes_audit(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    client.post("/settings/2fa/enable-totp")
    actions = [r["action"] for r in db.list_audit_events()]
    assert "user.2fa_setup_started" in actions


def test_role_change_writes_audit(client, db):
    org = make_org(db, "Acme")
    owner = make_user(db, username="o")
    add_membership(db, owner["id"], org["id"], role="owner")
    target = make_user(db, username="t")
    add_membership(db, target["id"], org["id"], role="user")
    login_as(client, owner, current_org_id=org["id"])
    resp = client.post(
        f"/settings/members/{target['id']}/role",
        data={"role": "manager"},
    )
    assert resp.status_code in (200, 302)
    actions = [
        r["action"] for r in db.list_audit_events(org_id=org["id"])
    ]
    assert "org.role_changed" in actions


def test_member_remove_writes_audit(client, db):
    org = make_org(db, "Acme")
    owner = make_user(db, username="o")
    add_membership(db, owner["id"], org["id"], role="owner")
    target = make_user(db, username="t")
    add_membership(db, target["id"], org["id"], role="user")
    login_as(client, owner, current_org_id=org["id"])
    resp = client.post(f"/settings/members/{target['id']}/remove")
    assert resp.status_code in (200, 302)
    actions = [
        r["action"] for r in db.list_audit_events(org_id=org["id"])
    ]
    assert "org.member_removed" in actions


def test_logout_writes_audit(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    client.post("/logout")
    assert any(
        r["action"] == "user.logout" for r in db.list_audit_events()
    )
