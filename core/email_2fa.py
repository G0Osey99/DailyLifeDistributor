"""Email-based 6-digit single-use 2FA codes (10-minute TTL).

Codes are bcrypt-hashed at rest. `generate_login_code` returns the plain
code AND emails it to the user (so the test fixture can capture it).
`verify_login_code` is constant-time per row via bcrypt.checkpw.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt

from core import db as _db
from core import email as _email

_TTL = timedelta(minutes=10)
_BCRYPT_ROUNDS = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_login_code(user_id: int) -> str:
    """Mint a fresh 6-digit code, store hashed, email plaintext to the user.

    Returns the plain code so callers (and tests) can introspect it.
    """
    code = f"{secrets.randbelow(10**6):06d}"
    h = bcrypt.hashpw(
        code.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    ).decode("ascii")
    now = _now()
    _db.insert_email_2fa_code(
        user_id=user_id,
        code_hash=h,
        expires_at=(now + _TTL).isoformat(),
        created_at=now.isoformat(),
    )
    user = _db.get_user_by_id(user_id)
    if user:
        try:
            _email.send(
                "2fa_code",
                to=user["email"],
                code=code,
                username=user["username"],
            )
        except Exception:
            # Non-fatal: code is still in DB; user can request another.
            pass
    return code


def verify_login_code(user_id: int, code: str) -> bool:
    """True iff `code` matches an unused, unexpired row for `user_id`."""
    if not code or not code.isdigit() or len(code) != 6:
        return False
    candidate = code.encode("utf-8")
    rows = _db.get_unused_email_2fa_codes(user_id)
    now_iso = _now().isoformat()
    for row in rows:
        if row["expires_at"] < now_iso:
            continue
        try:
            if bcrypt.checkpw(candidate, row["code_hash"].encode("ascii")):
                _db.mark_email_2fa_code_used(row["id"])
                return True
        except Exception:
            continue
    return False
