"""Phase γ Task 23: /admin/audit-log cross-org search (program-owner only)."""
from __future__ import annotations

from core import audit
from tests.helpers import login_as, make_org, make_user


def test_program_owner_sees_all_orgs(client, db):
    a = make_org(db, "Acme")
    b = make_org(db, "Beta", slug="beta")
    audit.write_event(action="user.login", actor_user_id=1, org_id=a["id"])
    audit.write_event(action="user.login", actor_user_id=2, org_id=b["id"])
    po = make_user(db, username="po", program_owner=True)
    login_as(client, po)
    resp = client.get("/admin/audit-log")
    body = resp.get_data(as_text=True)
    assert body.count("user.login") == 2


def test_non_program_owner_forbidden(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    resp = client.get("/admin/audit-log")
    assert resp.status_code == 403
