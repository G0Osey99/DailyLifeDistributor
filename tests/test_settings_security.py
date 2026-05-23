"""Phase γ Task 28: /settings/security new-device notification toggle."""
from __future__ import annotations

from tests.helpers import login_as, make_user


def test_get_settings_security_shows_pref(client, db):
    user = make_user(db, username="alice", notify_new_device=True)
    login_as(client, user)
    resp = client.get("/settings/security")
    body = resp.get_data(as_text=True)
    assert "Email me on new device sign-ins" in body
    assert "checked" in body


def test_post_disables_pref(client, db):
    user = make_user(db, username="alice", notify_new_device=True)
    login_as(client, user)
    resp = client.post("/settings/security", data={})  # checkbox unchecked
    assert resp.status_code in (200, 302)
    assert db.get_user_by_id(user["id"])["notify_new_device"] == 0


def test_post_enables_pref(client, db):
    user = make_user(db, username="alice", notify_new_device=False)
    login_as(client, user)
    resp = client.post(
        "/settings/security",
        data={"notify_new_device": "1"},
    )
    assert resp.status_code in (200, 302)
    assert db.get_user_by_id(user["id"])["notify_new_device"] == 1
