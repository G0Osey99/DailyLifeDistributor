"""Member-management route tests (list, invite, role-change, remove)."""
from __future__ import annotations

import pytest

from core import db, invitations


def test_members_page_renders_for_owner(client_owner, monkeypatch):
    monkeypatch.setattr("core.email.send", lambda *a, **k: True)
    client_owner.post(
        "/settings/members/invite",
        data={"email": "p@x.com", "role": "user"},
    )
    r = client_owner.get("/settings/members")
    assert r.status_code == 200
    assert b"p@x.com" in r.data


def test_members_page_renders_for_manager(client_manager):
    r = client_manager.get("/settings/members")
    assert r.status_code == 200


def test_members_page_denies_user_role(client_user):
    r = client_user.get("/settings/members")
    assert r.status_code == 403


def test_anonymous_redirected_to_login(client):
    r = client.get("/settings/members", follow_redirects=False)
    assert r.status_code in (302, 401)


def test_owner_can_promote_user(client_owner):
    with db._get_conn() as c:
        cur = c.execute(
            "INSERT INTO users (username, email, password_hash, created_at) "
            "VALUES ('u_promote', 'u_promote@x', 'x', datetime('now'))"
        )
        new_uid = cur.lastrowid
        c.execute(
            "INSERT INTO org_memberships (user_id, org_id, role, joined_at) "
            "VALUES (?, 1, 'user', datetime('now'))",
            (new_uid,),
        )
        c.commit()
    r = client_owner.post(
        f"/settings/members/{new_uid}/role",
        data={"role": "manager"},
    )
    assert r.status_code in (200, 302)
    with db._get_conn() as c:
        row = c.execute(
            "SELECT role FROM org_memberships WHERE user_id = ?",
            (new_uid,),
        ).fetchone()
    assert row["role"] == "manager"


def test_sole_owner_cannot_demote_self(client_owner):
    with client_owner.session_transaction() as s:
        uid = s["user_id"]
    r = client_owner.post(
        f"/settings/members/{uid}/role",
        data={"role": "manager"},
    )
    assert r.status_code == 400


def test_owner_can_remove_user(client_owner):
    with db._get_conn() as c:
        cur = c.execute(
            "INSERT INTO users (username, email, password_hash, created_at) "
            "VALUES ('u_remove', 'u_remove@x', 'x', datetime('now'))"
        )
        new_uid = cur.lastrowid
        c.execute(
            "INSERT INTO org_memberships (user_id, org_id, role, joined_at) "
            "VALUES (?, 1, 'user', datetime('now'))",
            (new_uid,),
        )
        c.commit()
    r = client_owner.post(f"/settings/members/{new_uid}/remove")
    assert r.status_code in (200, 302)
    with db._get_conn() as c:
        gone = c.execute(
            "SELECT 1 FROM org_memberships WHERE user_id=? AND org_id=1",
            (new_uid,),
        ).fetchone()
    assert gone is None


def test_manager_cannot_remove_manager(client_manager):
    with db._get_conn() as c:
        cur = c.execute(
            "INSERT INTO users (username, email, password_hash, created_at) "
            "VALUES ('mgr_other', 'mgr_other@x', 'x', datetime('now'))"
        )
        new_uid = cur.lastrowid
        c.execute(
            "INSERT INTO org_memberships (user_id, org_id, role, joined_at) "
            "VALUES (?, 1, 'manager', datetime('now'))",
            (new_uid,),
        )
        c.commit()
    r = client_manager.post(f"/settings/members/{new_uid}/remove")
    assert r.status_code == 403


def test_manager_cannot_revoke_owner_invite(client_owner, client_manager, monkeypatch):
    monkeypatch.setattr("core.email.send", lambda *a, **k: True)
    # Owner invites a Manager.
    client_owner.post(
        "/settings/members/invite",
        data={"email": "mgr@x.com", "role": "manager"},
    )
    [pending] = invitations.list_pending_invitations(1)
    # Manager (different user) tries to revoke that owner-issued manager invite.
    r = client_manager.post(f"/settings/members/{pending['id']}/revoke")
    assert r.status_code == 403


def test_invite_spam_guard_caps_pending(client_owner, monkeypatch):
    monkeypatch.setattr("core.email.send", lambda *a, **k: True)
    for _ in range(3):
        client_owner.post(
            "/settings/members/invite",
            data={"email": "spam@x.com", "role": "user"},
        )
    pending = invitations.list_pending_invitations(1)
    assert len(pending) == 3
    # 4th attempt is silently rejected (flash + redirect, no new row).
    client_owner.post(
        "/settings/members/invite",
        data={"email": "spam@x.com", "role": "user"},
    )
    pending = invitations.list_pending_invitations(1)
    assert len(pending) == 3
