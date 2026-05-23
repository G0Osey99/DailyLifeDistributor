"""Phase γ Task 2 + 3: TOTP primitives + Fernet-at-rest storage."""
from __future__ import annotations

import base64

import pyotp

from core import totp


def test_gen_secret_is_base32_16chars():
    s = totp.gen_secret()
    assert len(s) == 16
    # base32 decode must not throw
    base64.b32decode(s)


def test_build_provisioning_uri_contains_username_and_issuer():
    uri = totp.build_provisioning_uri("JBSWY3DPEHPK3PXP", "alice")
    assert "alice" in uri
    assert "Daily%20Life%20Distributor" in uri
    assert uri.startswith("otpauth://totp/")


def test_verify_totp_accepts_current_code():
    s = totp.gen_secret()
    code = pyotp.TOTP(s).now()
    assert totp.verify_totp(s, code) is True


def test_verify_totp_rejects_garbage():
    s = totp.gen_secret()
    assert totp.verify_totp(s, "000000") is False
    assert totp.verify_totp(s, "abc") is False
    assert totp.verify_totp(s, "") is False


def test_encrypt_decrypt_roundtrip():
    plain = totp.gen_secret()
    enc = totp.encrypt_secret_for_storage(plain)
    assert enc != plain
    assert totp.decrypt_secret_from_storage(enc) == plain


def test_decrypt_garbage_returns_none():
    assert totp.decrypt_secret_from_storage("not-a-fernet-token") is None
    assert totp.decrypt_secret_from_storage("") is None
