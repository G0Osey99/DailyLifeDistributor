"""Phase γ Task 27: email on first sighting of (user, ip)."""
from __future__ import annotations

from core import login_notifications
from tests.helpers import make_user


def test_first_sighting_emails(db, captured_emails):
    user = make_user(db, username="alice", email="a@x.com")
    login_notifications.notify_if_new_device(user["id"], "1.2.3.4", "TestUA")
    assert any(
        m["template"] == "login_new_device" and "a@x.com" in m["to"]
        for m in captured_emails
    )


def test_second_sighting_silent(db, captured_emails):
    user = make_user(db, username="alice", email="a@x.com")
    login_notifications.notify_if_new_device(user["id"], "1.2.3.4", "TestUA")
    captured_emails.clear()
    login_notifications.notify_if_new_device(user["id"], "1.2.3.4", "TestUA")
    assert not any(m["template"] == "login_new_device" for m in captured_emails)


def test_disabled_preference_suppresses(db, captured_emails):
    user = make_user(
        db, username="alice", email="a@x.com", notify_new_device=False,
    )
    login_notifications.notify_if_new_device(user["id"], "1.2.3.4", "TestUA")
    assert not any(m["template"] == "login_new_device" for m in captured_emails)


def test_different_ip_emails_again(db, captured_emails):
    user = make_user(db, username="alice", email="a@x.com")
    login_notifications.notify_if_new_device(user["id"], "1.2.3.4", "UA")
    captured_emails.clear()
    login_notifications.notify_if_new_device(user["id"], "5.6.7.8", "UA")
    assert any(m["template"] == "login_new_device" for m in captured_emails)
