"""Invitation tokens + CRUD.

Tokens are signed with itsdangerous using a secret derived from
SECRET_ENC_KEY (NOT the Fernet key itself), so rotating the Fernet
key rotates the signing secret as a side effect. We hash the signed
raw token before persisting (token_hash UNIQUE) so a DB compromise
can't surface live tokens.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from core import db

_SALT = "dld.invitations.v1"
_MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # 7 days


def _serializer() -> URLSafeTimedSerializer:
    enc_key = os.environ.get("SECRET_ENC_KEY")
    if not enc_key:
        raise RuntimeError("SECRET_ENC_KEY required to issue invitation tokens")
    # Derive a distinct signing secret: SHA-256(SECRET_ENC_KEY || _SALT).
    derived = hashlib.sha256(
        (enc_key + "|" + _SALT).encode("utf-8")
    ).hexdigest()
    return URLSafeTimedSerializer(secret_key=derived, salt=_SALT)


def issue_token(invitation_id: int) -> str:
    return _serializer().dumps(int(invitation_id))


def verify_token(raw: str) -> Optional[int]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = _serializer().loads(raw, max_age=_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    try:
        return int(payload)
    except (TypeError, ValueError):
        return None


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_invitation(
    org_id: int,
    inviter_user_id: int,
    email: str,
    role: str,
    ttl_days: int = 7,
) -> tuple[int, str]:
    """Create an invitation row + return (invitation_id, raw_token).

    The token_hash column stores SHA-256(raw_token). The raw token is
    returned to the caller exactly once so it can be emailed.
    """
    if role not in ("owner", "manager", "user"):
        raise ValueError(f"invalid role: {role}")
    import secrets as _pysecrets
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=ttl_days)
    # Placeholder token_hash uses a 32-byte random hex so the UNIQUE
    # constraint can't collide with a concurrent insert; we overwrite
    # with the real signed-token hash once we know the rowid.
    placeholder = "placeholder-" + _pysecrets.token_hex(32)
    with db._get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO invitations
               (org_id, inviter_user_id, email, role,
                token_hash, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                int(org_id), int(inviter_user_id),
                email.strip().lower(), role,
                placeholder,
                expires.isoformat(), now.isoformat(),
            ),
        )
        inv_id = int(cur.lastrowid)
        raw = issue_token(inv_id)
        conn.execute(
            "UPDATE invitations SET token_hash = ? WHERE id = ?",
            (_token_hash(raw), inv_id),
        )
        conn.commit()
    return inv_id, raw


def revoke_invitation(invitation_id: int) -> bool:
    now = _now_iso()
    with db._get_conn() as conn:
        cur = conn.execute(
            "UPDATE invitations SET revoked_at = ? "
            "WHERE id = ? AND revoked_at IS NULL AND accepted_at IS NULL",
            (now, invitation_id),
        )
        conn.commit()
        return cur.rowcount > 0


def accept_invitation(invitation_id: int, user_id: int) -> bool:
    """Atomically mark the invitation accepted and insert the membership.

    Returns False when:
      - invitation does not exist,
      - already accepted,
      - revoked,
      - expired.
    """
    now = _now_iso()
    now_dt = datetime.now(timezone.utc)
    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT org_id, role, accepted_at, revoked_at, expires_at "
            "FROM invitations WHERE id = ?",
            (invitation_id,),
        ).fetchone()
        if row is None or row["accepted_at"] or row["revoked_at"]:
            return False
        try:
            exp = datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            return False
        if exp < now_dt:
            return False
        conn.execute(
            """INSERT OR IGNORE INTO org_memberships
               (user_id, org_id, role, joined_at)
               VALUES (?, ?, ?, ?)""",
            (int(user_id), int(row["org_id"]), row["role"], now),
        )
        conn.execute(
            "UPDATE invitations SET accepted_at = ? WHERE id = ?",
            (now, invitation_id),
        )
        conn.commit()
    return True


def list_pending_invitations(org_id: int) -> list[dict]:
    with db._get_conn() as conn:
        rows = conn.execute(
            """SELECT id, email, role, created_at, expires_at
               FROM invitations
               WHERE org_id = ?
                 AND accepted_at IS NULL
                 AND revoked_at IS NULL
               ORDER BY created_at DESC""",
            (org_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_invitations_by_email(
    email: str, org_id: int, status: str = "pending"
) -> list[dict]:
    """Return invitation rows for *email* in *org_id*.

    status:
      'pending' — neither accepted nor revoked.
      'all'     — all rows regardless of status (used for audit).
    """
    email = email.strip().lower()
    with db._get_conn() as conn:
        if status == "pending":
            rows = conn.execute(
                "SELECT id, role, created_at, expires_at "
                "FROM invitations "
                "WHERE email = ? AND org_id = ? "
                "  AND accepted_at IS NULL AND revoked_at IS NULL",
                (email, org_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, role, created_at, expires_at, "
                "       accepted_at, revoked_at "
                "FROM invitations WHERE email = ? AND org_id = ?",
                (email, org_id),
            ).fetchall()
    return [dict(r) for r in rows]


def get_invitation_with_org(invitation_id: int) -> Optional[dict]:
    """Return invitation joined with org name, or None."""
    with db._get_conn() as conn:
        row = conn.execute(
            """SELECT i.*, o.name AS org_name
               FROM invitations i
               JOIN organizations o ON o.id = i.org_id
               WHERE i.id = ?""",
            (invitation_id,),
        ).fetchone()
    return dict(row) if row else None
