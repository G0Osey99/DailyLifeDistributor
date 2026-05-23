"""Phase γ Task 24: recovery_request submit + rate-limit + Owner emails."""
from __future__ import annotations

from freezegun import freeze_time

from core import recovery_request
from tests.helpers import add_membership, make_org, make_user


def test_submit_creates_row_and_emails_all_owners(db, captured_emails):
    org = make_org(db, "Acme")
    user = make_user(db, username="alice", email="alice@example.com")
    add_membership(db, user["id"], org["id"], role="user")
    o1 = make_user(db, username="o1", email="o1@x.com")
    add_membership(db, o1["id"], org["id"], role="owner")
    o2 = make_user(db, username="o2", email="o2@x.com")
    add_membership(db, o2["id"], org["id"], role="owner")
    rid = recovery_request.submit_request("alice", note="lost my phone")
    assert rid > 0
    targets = [m["to"] for m in captured_emails if m["template"] == "recovery_request"]
    assert "o1@x.com" in targets and "o2@x.com" in targets


def test_unknown_username_silently_succeeds(db, captured_emails):
    rid = recovery_request.submit_request("ghost", note="hi")
    assert rid is None
    assert not any(m["template"] == "recovery_request" for m in captured_emails)


def test_rate_limit_one_per_24h(db, captured_emails):
    org = make_org(db, "Acme")
    user = make_user(db, username="alice")
    add_membership(db, user["id"], org["id"], role="user")
    with freeze_time("2026-05-23 10:00:00"):
        r1 = recovery_request.submit_request("alice", note="first")
        assert r1 > 0
    with freeze_time("2026-05-23 22:00:00"):  # 12h later — still in window
        r2 = recovery_request.submit_request("alice", note="second")
        assert r2 is None
    with freeze_time("2026-05-24 11:00:00"):  # >24h later
        r3 = recovery_request.submit_request("alice", note="later")
        assert r3 > 0
