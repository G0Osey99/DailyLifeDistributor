"""Phase γ Task 29: org-level Require-2FA toggle + before_request enforcement."""
from __future__ import annotations

from tests.helpers import add_membership, login_as, make_org, make_user


def test_only_owner_can_flip_require_2fa(client, db):
    org = make_org(db, "Acme")
    mgr = make_user(db, username="m")
    add_membership(db, mgr["id"], org["id"], role="manager")
    login_as(client, mgr, current_org_id=org["id"])
    resp = client.post(
        "/settings/org/require-2fa", data={"enabled": "1"},
    )
    assert resp.status_code == 403
    assert db.get_org(org["id"])["require_2fa"] == 0


def test_owner_flips_require_2fa_writes_audit(client, db):
    org = make_org(db, "Acme")
    owner = make_user(db, username="o")
    add_membership(db, owner["id"], org["id"], role="owner")
    login_as(client, owner, current_org_id=org["id"])
    resp = client.post(
        "/settings/org/require-2fa", data={"enabled": "1"},
    )
    assert resp.status_code in (200, 302)
    assert db.get_org(org["id"])["require_2fa"] == 1
    actions = [r["action"] for r in db.list_audit_events(org_id=org["id"])]
    assert "org.settings_changed" in actions


def test_user_without_2fa_redirected_when_org_requires_it(client, db):
    org = make_org(db, "Acme", require_2fa=True)
    user = make_user(
        db, username="u", totp_enabled=False, email_2fa_enabled=False,
    )
    add_membership(db, user["id"], org["id"], role="user")
    login_as(client, user, current_org_id=org["id"])
    resp = client.get("/dashboard")
    assert resp.status_code == 302
    assert "/settings/2fa" in resp.headers["Location"]


def test_user_with_totp_passes_enforcement(client, db):
    org = make_org(db, "Acme", require_2fa=True)
    user = make_user(db, username="u", totp_enabled=True)
    add_membership(db, user["id"], org["id"], role="user")
    login_as(client, user, current_org_id=org["id"])
    resp = client.get("/dashboard")
    assert resp.status_code == 200
