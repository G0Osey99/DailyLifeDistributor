"""Tests for core.invitations: signed-token issue/verify, CRUD, accept flow."""
from __future__ import annotations

import pytest

from core import invitations


def test_issue_and_verify_token_roundtrip(monkeypatch):
    raw = invitations.issue_token(invitation_id=42)
    assert isinstance(raw, str) and len(raw) > 20
    payload = invitations.verify_token(raw)
    assert payload == 42


def test_verify_token_rejects_tampered():
    raw = invitations.issue_token(7)
    tampered = raw[:-2] + ("AA" if raw[-2:] != "AA" else "BB")
    assert invitations.verify_token(tampered) is None
    assert invitations.verify_token("not-a-token") is None


# -------- CRUD ----------------------------------------------------------

def _seed_org_and_user(uid=1, oid=1):
    from core import db
    with db._get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO organizations "
            "(id, name, slug, plan, created_at) "
            "VALUES (?, ?, ?, 'free', datetime('now'))",
            (oid, f"Org{oid}", f"org-{oid}"),
        )
        c.execute(
            "INSERT OR IGNORE INTO users "
            "(id, username, email, password_hash, created_at) "
            "VALUES (?, ?, ?, 'x', datetime('now'))",
            (uid, f"u{uid}", f"u{uid}@x.com"),
        )
        c.commit()


def test_create_and_list_pending():
    _seed_org_and_user()
    inv_id, token = invitations.create_invitation(
        org_id=1, inviter_user_id=1, email="a@b.com", role="user"
    )
    assert isinstance(inv_id, int)
    assert invitations.verify_token(token) == inv_id
    rows = invitations.list_pending_invitations(org_id=1)
    assert len(rows) == 1 and rows[0]["email"] == "a@b.com" and rows[0]["role"] == "user"


def test_create_rejects_bad_role():
    _seed_org_and_user()
    with pytest.raises(ValueError):
        invitations.create_invitation(1, 1, "x@y.com", "godking")


def test_revoke_then_accept_fails():
    _seed_org_and_user()
    inv_id, _ = invitations.create_invitation(1, 1, "x@y.com", "user")
    assert invitations.revoke_invitation(inv_id) is True
    # Need a user to accept
    from core import db
    with db._get_conn() as c:
        c.execute(
            "INSERT INTO users (username, email, password_hash, created_at) "
            "VALUES ('aux', 'aux@x.com', 'x', datetime('now'))"
        )
        c.commit()
        new_uid = c.execute("SELECT id FROM users WHERE username='aux'").fetchone()["id"]
    assert invitations.accept_invitation(inv_id, user_id=new_uid) is False


def test_accept_creates_membership():
    _seed_org_and_user()
    inv_id, _ = invitations.create_invitation(1, 1, "x@y.com", "manager")
    from core import db
    with db._get_conn() as c:
        c.execute(
            "INSERT INTO users (username, email, password_hash, created_at) "
            "VALUES ('newby', 'n@x.com', 'x', datetime('now'))"
        )
        c.commit()
        new_uid = c.execute("SELECT id FROM users WHERE username='newby'").fetchone()["id"]
    assert invitations.accept_invitation(inv_id, user_id=new_uid) is True
    with db._get_conn() as c:
        row = c.execute(
            "SELECT role FROM org_memberships WHERE user_id=? AND org_id=1",
            (new_uid,),
        ).fetchone()
    assert row["role"] == "manager"


def test_list_invitations_by_email_counts_pending_only():
    _seed_org_and_user()
    i1, _ = invitations.create_invitation(1, 1, "spam@x.com", "user")
    i2, _ = invitations.create_invitation(1, 1, "spam@x.com", "user")
    invitations.revoke_invitation(i1)
    pending = invitations.list_invitations_by_email("spam@x.com", org_id=1, status="pending")
    assert [p["id"] for p in pending] == [i2]
