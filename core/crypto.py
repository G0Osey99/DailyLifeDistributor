"""Symmetric encryption for secrets at rest, keyed by an env-var master key.

The master key lives in SECRET_ENC_KEY (a urlsafe-base64 32-byte Fernet key).
The app fails closed if it is missing or malformed — better a clear startup
error than silently storing or returning garbage.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

_ENV_VAR = "SECRET_ENC_KEY"

_GENERATE_HINT = (
    'Generate one with: python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())" and set it as the '
    f"{_ENV_VAR} environment variable."
)


class MasterKeyError(RuntimeError):
    """SECRET_ENC_KEY is missing or not a valid Fernet key."""


class DecryptError(RuntimeError):
    """Ciphertext could not be decrypted (wrong key or tampered data)."""


def _load_fernet() -> Fernet:
    raw = (os.environ.get(_ENV_VAR) or "").strip()
    if not raw:
        raise MasterKeyError(f"{_ENV_VAR} is not set. {_GENERATE_HINT}")
    try:
        return Fernet(raw.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise MasterKeyError(
            f"{_ENV_VAR} is not a valid Fernet key. {_GENERATE_HINT}"
        ) from exc


def validate_master_key() -> None:
    """Raise MasterKeyError if the key is missing/invalid. Call at startup."""
    _load_fernet()


def encrypt(data: bytes) -> bytes:
    return _load_fernet().encrypt(data)


def decrypt(token: bytes) -> bytes:
    try:
        return _load_fernet().decrypt(token)
    except InvalidToken as exc:
        raise DecryptError("Could not decrypt secret (wrong key or tampered).") from exc
