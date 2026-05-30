"""Email-based 6-digit single-use 2FA codes (10-minute TTL).

Codes are bcrypt-hashed at rest. `generate_login_code` returns the plain
code AND emails it to the user (so the test fixture can capture it).
`verify_login_code` is constant-time per row via bcrypt.checkpw.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt

from core import db as _db
from core import email as _email

log = logging.getLogger(__name__)

_TTL = timedelta(minutes=10)
_BCRYPT_ROUNDS = 10

# SEC-005: per-user send rate limit. The send paths (login email-2FA GET,
# settings send-email-code / enable-email) are otherwise unthrottled, so a
# held partial token or a logged-in user could spam the victim's inbox and
# burn Resend quota. Cap the number of codes minted+emailed per window.
_RATE_MAX = 3
_RATE_WINDOW = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_login_code(user_id: int) -> str | None:
    """Mint a fresh 6-digit code, store hashed, email plaintext to the user.

    Returns the plain code, or ``None`` when the per-user send rate limit
    (SEC-005) is hit — in which case no new code is minted or emailed and the
    user falls back to the most recent code already sent within the window.
    Callers ignore the return value; tests introspect it.
    """
    since_iso = (_now() - _RATE_WINDOW).isoformat()
    if _db.count_email_2fa_codes_since(user_id, since_iso) >= _RATE_MAX:
        log.warning(
            "email-2FA send rate limit hit for user_id=%s (>=%d in %s); "
            "suppressing extra code", user_id, _RATE_MAX, _RATE_WINDOW,
        )
        return None
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
            # Log so a broken Resend setup is visible — silent failure
            # here is a real security UX hole.
            log.warning(
                "2FA email send to %s failed (user_id=%s); user can "
                "request a fresh code", user["email"], user_id, exc_info=True,
            )
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
            # bcrypt.checkpw raises on malformed hash bytes — a corrupt
            # row shouldn't poison the verify (other rows might match)
            # but should be visible to ops so it can be cleaned up.
            log.warning(
                "bcrypt.checkpw failed on email_2fa_code id=%s; "
                "row may be corrupt", row.get("id"), exc_info=True,
            )
            continue
    return False
