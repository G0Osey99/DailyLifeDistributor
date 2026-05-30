"""Phase γ Task 17: email-based 6-digit 2FA code."""
from __future__ import annotations

from freezegun import freeze_time

from core import email_2fa
from tests.helpers import make_user


def test_generate_sends_and_stores_hash(db, captured_emails):
    user = make_user(db, username="eve", email="eve@example.com")
    code = email_2fa.generate_login_code(user["id"])
    assert len(code) == 6 and code.isdigit()
    msg = captured_emails[-1]
    assert msg["template"] == "2fa_code"
    assert "eve@example.com" in msg["to"]
    assert msg["vars"]["code"] == code
    rows = db.get_unused_email_2fa_codes(user["id"])
    assert len(rows) == 1
    assert code not in rows[0]["code_hash"]  # hashed


def test_verify_correct_code(db, captured_emails):
    user = make_user(db, username="eve", email="eve@example.com")
    code = email_2fa.generate_login_code(user["id"])
    assert email_2fa.verify_login_code(user["id"], code) is True
    # Second use rejected (single-use)
    assert email_2fa.verify_login_code(user["id"], code) is False


def test_verify_expired_code_returns_false(db, captured_emails):
    user = make_user(db, username="eve", email="eve@example.com")
    with freeze_time("2026-05-23 12:00:00"):
        code = email_2fa.generate_login_code(user["id"])
    with freeze_time("2026-05-23 12:11:00"):  # 11 minutes later
        assert email_2fa.verify_login_code(user["id"], code) is False


def test_verify_garbage_returns_false(db, captured_emails):
    user = make_user(db, username="eve", email="eve@example.com")
    email_2fa.generate_login_code(user["id"])
    assert email_2fa.verify_login_code(user["id"], "abc") is False
    assert email_2fa.verify_login_code(user["id"], "0000000") is False
    assert email_2fa.verify_login_code(user["id"], "") is False


def test_generate_rate_limited_per_user(db, captured_emails):
    """SEC-005: at most _RATE_MAX codes per user per window. The next call is
    suppressed (returns None, no new code, no extra email)."""
    user = make_user(db, username="eve", email="eve@example.com")
    with freeze_time("2026-05-23 12:00:00"):
        for _ in range(email_2fa._RATE_MAX):
            assert email_2fa.generate_login_code(user["id"]) is not None
        sent_before = len(captured_emails)
        # The next one within the window is suppressed.
        assert email_2fa.generate_login_code(user["id"]) is None
        assert len(captured_emails) == sent_before  # no extra email
        assert len(db.get_unused_email_2fa_codes(user["id"])) == email_2fa._RATE_MAX
    # After the window passes, sending resumes.
    with freeze_time("2026-05-23 12:11:00"):
        assert email_2fa.generate_login_code(user["id"]) is not None
