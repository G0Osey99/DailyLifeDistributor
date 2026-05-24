"""Routes that change account security must 409 under impersonation."""
from __future__ import annotations

import pytest

from core import db, user_store, org_store


FORBIDDEN_ROUTES = [
    # blueprints/twofa.py
    ("POST", "/settings/2fa/enable-totp"),
    ("POST", "/settings/2fa/verify-totp"),
    ("POST", "/settings/2fa/enable-email"),
    ("POST", "/settings/2fa/send-email-code"),
    ("POST", "/settings/2fa/disable"),
    ("POST", "/settings/2fa/recovery-codes/regenerate"),
    # blueprints/settings.py
    ("POST", "/settings/change-password"),
    # blueprints/members.py — role change in the impersonated org
    ("POST", "/settings/members/1/role"),
    # blueprints/recovery.py — admin approval flow
    ("GET",  "/admin-actions/recovery/1/approve"),
    # blueprints/auth.py — own-password set; impersonator must not retarget it
    ("POST", "/login/first-password-set"),
]


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


@pytest.mark.parametrize("method,path", FORBIDDEN_ROUTES)
def test_route_409s_under_impersonation(app, method, path):
    org = org_store.create_org(name="O", slug="o")
    target = org_store.create_org(name="T", slug="t")
    po = user_store.create_user(
        username="po", email="po@x", password="pw1234567", program_owner=True,
    )
    org_store.add_membership(user_id=po["id"], org_id=org["id"], role="owner")
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = po["id"]
        s["current_org_id"] = org["id"]
        s["acting_as_org_id"] = target["id"]
        s["permission_2fa_passed"] = True
    res = client.open(path, method=method)
    assert res.status_code == 409, f"{method} {path} should 409, got {res.status_code}"
