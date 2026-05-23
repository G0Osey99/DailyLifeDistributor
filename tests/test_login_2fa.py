"""Phase γ Tasks 14-16: 2FA second-factor login flow."""
from __future__ import annotations

import pyotp

from core import recovery as _recovery
from core import totp as _totp
from tests.helpers import make_user


def test_password_only_user_logs_in_directly(client, db):
    make_user(db, username="alice", password="hunter22hunter22")
    resp = client.post(
        "/login",
        data={"username": "alice", "password": "hunter22hunter22"},
    )
    assert resp.status_code == 302
    # Should land on the dashboard, not a 2FA URL.
    assert "/login/2fa" not in resp.headers["Location"]


def test_totp_user_redirected_to_login_2fa(client, db):
    secret = "JBSWY3DPEHPK3PXP"
    make_user(
        db, username="bob", password="hunter22hunter22",
        totp_enabled=True,
        totp_secret_encrypted=_totp.encrypt_secret_for_storage(secret),
    )
    resp = client.post(
        "/login",
        data={"username": "bob", "password": "hunter22hunter22"},
    )
    assert resp.status_code == 302
    assert "/login/2fa" in resp.headers["Location"]
    assert "tok=" in resp.headers["Location"]


def test_email_only_user_redirected_to_email_2fa(client, db):
    make_user(
        db, username="eve", password="hunter22hunter22",
        email_2fa_enabled=True,
    )
    resp = client.post(
        "/login",
        data={"username": "eve", "password": "hunter22hunter22"},
    )
    assert resp.status_code == 302
    assert "/login/email-2fa" in resp.headers["Location"]


def test_login_2fa_post_totp_finalizes_session(client, db):
    secret = "JBSWY3DPEHPK3PXP"
    user = make_user(
        db, username="bob", password="hunter22hunter22",
        totp_enabled=True,
        totp_secret_encrypted=_totp.encrypt_secret_for_storage(secret),
    )
    resp = client.post(
        "/login",
        data={"username": "bob", "password": "hunter22hunter22"},
    )
    tok = resp.headers["Location"].split("tok=")[1]
    code = pyotp.TOTP(secret).now()
    r2 = client.post("/login/2fa", data={"tok": tok, "code": code})
    assert r2.status_code == 302
    with client.session_transaction() as s:
        assert s["user_id"] == user["id"]


def test_login_2fa_post_recovery_code_finalizes_and_marks_used(client, db):
    secret = "JBSWY3DPEHPK3PXP"
    user = make_user(
        db, username="bob", password="hunter22hunter22",
        totp_enabled=True,
        totp_secret_encrypted=_totp.encrypt_secret_for_storage(secret),
    )
    codes = _recovery.generate_recovery_codes(user["id"])
    resp = client.post(
        "/login",
        data={"username": "bob", "password": "hunter22hunter22"},
    )
    tok = resp.headers["Location"].split("tok=")[1]
    r2 = client.post("/login/2fa", data={"tok": tok, "code": codes[0]})
    assert r2.status_code == 302
    # Same code rejected on second try
    resp = client.post(
        "/login",
        data={"username": "bob", "password": "hunter22hunter22"},
    )
    tok2 = resp.headers["Location"].split("tok=")[1]
    r3 = client.post("/login/2fa", data={"tok": tok2, "code": codes[0]})
    assert r3.status_code == 400


def test_login_email_2fa_flow(client, db, captured_emails):
    user = make_user(
        db, username="eve", password="hunter22hunter22",
        email_2fa_enabled=True, email="eve@example.com",
    )
    resp = client.post(
        "/login",
        data={"username": "eve", "password": "hunter22hunter22"},
    )
    tok = resp.headers["Location"].split("tok=")[1]
    r1 = client.get(f"/login/email-2fa?tok={tok}")
    assert r1.status_code == 200
    msg = next(m for m in captured_emails if m["template"] == "2fa_code")
    code = msg["vars"]["code"]
    # Re-token from the rendered page form
    body = r1.get_data(as_text=True)
    import re
    m = re.search(r'name="tok"\s+value="([^"]+)"', body)
    assert m
    new_tok = m.group(1)
    r2 = client.post("/login/email-2fa", data={"tok": new_tok, "code": code})
    assert r2.status_code == 302
    with client.session_transaction() as s:
        assert s["user_id"] == user["id"]
