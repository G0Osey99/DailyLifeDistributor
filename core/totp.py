"""RFC 6238 TOTP helpers — gen / URI / verify, plus Fernet at-rest encryption.

Why 16-char (80-bit) secret instead of pyotp's default 32-char (160-bit):
80 bits matches the RFC 6238 reference and is what the spec asks for.
That's the only deviation from the pyotp defaults; verify_TOTP() still
uses pyotp's drift-tolerant verifier.
"""
from __future__ import annotations

import logging
import secrets

import pyotp

from core.crypto import _load_fernet as _get_fernet

log = logging.getLogger(__name__)

_ISSUER = "Daily Life Distributor"


def gen_secret() -> str:
    """Return a base32-encoded 16-char secret (80 bits)."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    return "".join(secrets.choice(alphabet) for _ in range(16))


def build_provisioning_uri(secret: str, username: str, issuer: str = _ISSUER) -> str:
    """Return the otpauth:// URI for QR rendering."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str, drift: int = 1) -> bool:
    """Verify a 6-digit TOTP code with ±drift 30-second steps."""
    if not code or not code.isdigit() or len(code) != 6:
        return False
    try:
        return pyotp.TOTP(secret).verify(code, valid_window=drift)
    except Exception:
        # pyotp raises on a malformed base32 secret. Don't crash auth
        # but log so a corrupt secret-store row is visible.
        log.warning("pyotp.TOTP.verify raised on stored secret",
                    exc_info=True)
        return False


def encrypt_secret_for_storage(plaintext_secret: str) -> str:
    """Encrypt a TOTP secret using the app's Fernet master key."""
    f = _get_fernet()
    return f.encrypt(plaintext_secret.encode("utf-8")).decode("ascii")


def decrypt_secret_from_storage(ciphertext: str) -> str | None:
    """Decrypt; return None on any failure (corrupt / wrong key).

    A wrong SECRET_ENC_KEY (rotated without re-encrypting) and a
    corrupt ciphertext row both surface here; logging the failure at
    warning is the only way ops sees this without staring at the
    "TOTP didn't work for anyone" support ticket.
    """
    if not ciphertext:
        return None
    try:
        f = _get_fernet()
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception:
        log.warning(
            "TOTP secret Fernet decrypt failed — wrong SECRET_ENC_KEY "
            "or corrupt ciphertext", exc_info=True,
        )
        return None
