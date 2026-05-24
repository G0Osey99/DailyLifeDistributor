"""Backup recovery codes — generate, verify (single-use), regenerate.

Codes are bcrypt-hashed at rest (cost-resistant). Plain codes are returned
ONCE at generation time; the database never sees them after that.
"""
from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timezone

import bcrypt

from core import db as _db

log = logging.getLogger(__name__)

_ALPHABET = string.ascii_uppercase + string.digits
_CODE_LEN = 8
_CODE_COUNT = 10
_BCRYPT_ROUNDS = 10


def _new_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_recovery_codes(user_id: int, count: int = _CODE_COUNT) -> list[str]:
    """Mint `count` codes, store bcrypt-hashed, return plain codes ONCE."""
    plain = [_new_code() for _ in range(count)]
    now = _now()
    for p in plain:
        h = bcrypt.hashpw(
            p.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
        ).decode("ascii")
        _db.insert_recovery_code(user_id=user_id, code_hash=h, created_at=now)
    return plain


def verify_recovery_code(user_id: int, code: str) -> bool:
    """Return True iff code matches an unused row; mark it used atomically."""
    if not code:
        return False
    candidate = code.strip().upper().encode("utf-8")
    rows = _db.list_recovery_codes(user_id)
    for row in rows:
        if row["used_at"] is not None:
            continue
        try:
            if bcrypt.checkpw(candidate, row["code_hash"].encode("ascii")):
                _db.mark_recovery_code_used(row["id"])
                return True
        except Exception:
            # Malformed hash row — keep iterating (other rows may match)
            # but log so ops can find + clean the corrupt entry.
            log.warning(
                "bcrypt.checkpw failed on recovery_code id=%s; "
                "row may be corrupt", row.get("id"), exc_info=True,
            )
            continue
    return False


def regenerate_codes(user_id: int) -> list[str]:
    """Purge any existing codes and mint a fresh set."""
    _db.delete_recovery_codes(user_id)
    return generate_recovery_codes(user_id)
