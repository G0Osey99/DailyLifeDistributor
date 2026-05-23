"""Phase γ Tasks 5-9, 13: /settings/2fa* routes."""
from __future__ import annotations

import pyotp

from core import recovery as _recovery
from core import totp as _totp
from tests.helpers import login_as, make_user


def test_get_settings_2fa_shows_disabled_state(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    resp = client.get("/settings/2fa")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Authenticator app" in body
    assert "Enable TOTP" in body
    assert "Enable email codes" in body


def test_get_settings_2fa_shows_enabled_state(client, db):
    user = make_user(db, username="bob", totp_enabled=True, email_2fa_enabled=True)
    login_as(client, user)
    resp = client.get("/settings/2fa")
    body = resp.get_data(as_text=True)
    assert "Disable" in body
    assert "Authenticator app: enabled" in body


def test_enable_totp_renders_qr_and_pending_secret(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    resp = client.post("/settings/2fa/enable-totp", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Scan with your authenticator app" in body
    assert '<img src="/settings/2fa/qrcode.png"' in body
    with client.session_transaction() as s:
        assert s["pending_totp_secret_enc"]


def test_verify_totp_good_code_enables_and_shows_recovery(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    client.post("/settings/2fa/enable-totp")
    with client.session_transaction() as s:
        enc = s["pending_totp_secret_enc"]
    secret = _totp.decrypt_secret_from_storage(enc)
    code = pyotp.TOTP(secret).now()
    resp = client.post("/settings/2fa/verify-totp", data={"code": code})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Save these recovery codes" in body
    row = db.get_user_by_id(user["id"])
    assert row["totp_enabled"] == 1
    assert row["totp_secret_encrypted"] == enc


def test_verify_totp_bad_code_does_not_enable(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    client.post("/settings/2fa/enable-totp")
    resp = client.post("/settings/2fa/verify-totp", data={"code": "000000"})
    assert resp.status_code == 400
    row = db.get_user_by_id(user["id"])
    assert row["totp_enabled"] == 0


def test_enable_email_2fa_flips_flag_and_sends(client, db, captured_emails):
    user = make_user(db, username="alice", email="alice@example.com")
    login_as(client, user)
    resp = client.post("/settings/2fa/enable-email")
    assert resp.status_code in (200, 302)
    assert db.get_user_by_id(user["id"])["email_2fa_enabled"] == 1
    assert any(
        m["template"] == "2fa_code" and "alice@example.com" in m["to"]
        for m in captured_emails
    )


def test_disable_totp_requires_current_code(client, db):
    secret = "JBSWY3DPEHPK3PXP"
    user = make_user(
        db, username="alice",
        totp_enabled=True,
        totp_secret_encrypted=_totp.encrypt_secret_for_storage(secret),
    )
    login_as(client, user)
    resp = client.post(
        "/settings/2fa/disable", data={"method": "totp", "code": "000000"},
    )
    assert resp.status_code == 400
    assert db.get_user_by_id(user["id"])["totp_enabled"] == 1


def test_disable_totp_with_valid_code_clears_secret(client, db):
    secret = "JBSWY3DPEHPK3PXP"
    user = make_user(
        db, username="alice",
        totp_enabled=True,
        totp_secret_encrypted=_totp.encrypt_secret_for_storage(secret),
    )
    login_as(client, user)
    code = pyotp.TOTP(secret).now()
    resp = client.post(
        "/settings/2fa/disable", data={"method": "totp", "code": code},
    )
    assert resp.status_code in (200, 302)
    row = db.get_user_by_id(user["id"])
    assert row["totp_enabled"] == 0
    assert row["totp_secret_encrypted"] is None


def test_get_recovery_codes_does_not_show_plaintext(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    _recovery.generate_recovery_codes(user["id"])
    resp = client.get("/settings/2fa/recovery-codes")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Recovery codes" in body
    assert "<code>" not in body  # no plaintext re-display
    assert "Regenerate" in body


def test_post_regenerate_shows_new_codes_once(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    _recovery.generate_recovery_codes(user["id"])
    resp = client.post("/settings/2fa/recovery-codes/regenerate")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert body.count("<code>") == 10
