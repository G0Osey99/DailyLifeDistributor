"""Phase γ Tasks 25-26: /recover + /admin-actions/recovery + /recover/reset."""
from __future__ import annotations

from core import totp as _totp
from core import user_store
from tests.helpers import add_membership, last_email, login_as, make_org, make_user


def test_post_recover_creates_request_and_emails_owners(client, db, captured_emails):
    org = make_org(db, "Acme")
    user = make_user(db, username="alice", email="alice@example.com")
    add_membership(db, user["id"], org["id"], role="user")
    owner = make_user(db, username="o", email="o@x.com")
    add_membership(db, owner["id"], org["id"], role="owner")
    resp = client.post("/recover", data={"username": "alice", "note": "lost phone"})
    assert resp.status_code in (200, 302)
    assert any(
        m["template"] == "recovery_request" and "o@x.com" in m["to"]
        for m in captured_emails
    )


def test_owner_clicks_approve_link_emails_reset(client, db, captured_emails):
    org = make_org(db, "Acme")
    user = make_user(db, username="alice", email="alice@example.com")
    add_membership(db, user["id"], org["id"], role="user")
    owner = make_user(db, username="o", email="o@x.com")
    add_membership(db, owner["id"], org["id"], role="owner")
    client.post("/recover", data={"username": "alice", "note": "lost phone"})
    rid = db.list_recovery_requests()[0]["id"]
    login_as(client, owner)
    resp = client.get(f"/admin-actions/recovery/{rid}/approve")
    assert resp.status_code in (200, 302)
    reset = last_email(captured_emails, "recovery_approved")
    assert "alice@example.com" in reset["to"]
    assert "reset_url" in reset["vars"]


def test_non_owner_cannot_approve(client, db, captured_emails):
    org = make_org(db, "Acme")
    user = make_user(db, username="alice", email="alice@example.com")
    add_membership(db, user["id"], org["id"], role="user")
    other = make_user(db, username="other", email="other@x.com")
    client.post("/recover", data={"username": "alice", "note": "x"})
    rid = db.list_recovery_requests()[0]["id"]
    login_as(client, other)
    resp = client.get(f"/admin-actions/recovery/{rid}/approve")
    assert resp.status_code == 403


def test_reset_with_valid_token_sets_password_and_clears_totp(client, db, captured_emails):
    org = make_org(db, "Acme")
    user = make_user(
        db, username="alice", email="alice@example.com",
        totp_enabled=True,
        totp_secret_encrypted=_totp.encrypt_secret_for_storage("JBSWY3DPEHPK3PXP"),
    )
    add_membership(db, user["id"], org["id"], role="user")
    owner = make_user(db, username="o", email="o@x.com")
    add_membership(db, owner["id"], org["id"], role="owner")
    client.post("/recover", data={"username": "alice", "note": "x"})
    rid = db.list_recovery_requests()[0]["id"]
    login_as(client, owner)
    client.get(f"/admin-actions/recovery/{rid}/approve")
    msg = last_email(captured_emails, "recovery_approved")
    token = msg["vars"]["reset_url"].split("token=")[1]
    # New client to drop the owner session
    r = client.post(
        f"/recover/reset?token={token}",
        data={"password": "newhunter22hunter", "password2": "newhunter22hunter"},
    )
    assert r.status_code in (200, 302)
    row = db.get_user_by_id(user["id"])
    assert user_store.verify_password(user["id"], "newhunter22hunter") is True
    assert row["totp_enabled"] == 0
    assert row["totp_secret_encrypted"] is None
    assert db.list_recovery_codes(user["id"]) == []


def test_reset_with_used_token_rejected(client, db, captured_emails):
    org = make_org(db, "Acme")
    user = make_user(db, username="alice", email="alice@example.com")
    add_membership(db, user["id"], org["id"], role="user")
    owner = make_user(db, username="o", email="o@x.com")
    add_membership(db, owner["id"], org["id"], role="owner")
    client.post("/recover", data={"username": "alice", "note": "x"})
    rid = db.list_recovery_requests()[0]["id"]
    login_as(client, owner)
    client.get(f"/admin-actions/recovery/{rid}/approve")
    msg = last_email(captured_emails, "recovery_approved")
    token = msg["vars"]["reset_url"].split("token=")[1]
    r1 = client.post(
        f"/recover/reset?token={token}",
        data={"password": "newhunter22hunter", "password2": "newhunter22hunter"},
    )
    assert r1.status_code in (200, 302)
    r2 = client.post(
        f"/recover/reset?token={token}",
        data={"password": "anotherhunter22", "password2": "anotherhunter22"},
    )
    assert r2.status_code == 400
