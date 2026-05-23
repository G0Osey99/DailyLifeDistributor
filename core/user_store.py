"""Argon2id-backed user store. Multi-tenant phase α.

password_changed_at is set to NULL on create. A NULL value forces a
password change on first login (verify_password returns False until
update_password() is called). This is the migration semantics: when we
seed the bootstrap user from INITIAL_ADMIN_PASSWORD, the program-owner
is forced to set a new password before they can actually log in.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash

from core import db

# OWASP-recommended Argon2id parameters (per spec).
_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=65536,
    parallelism=4,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def create_user(
    username: str,
    email: str,
    password: str,
    program_owner: bool = False,
) -> dict:
    """Insert a new user. Returns the inserted row.

    password_changed_at is set to NULL so verify_password() rejects the
    bootstrap password and forces a real change on first login.
    """
    pw_hash = hash_password(password)
    now = _now()
    with db._get_conn() as c:
        cur = c.execute(
            "INSERT INTO users (username, email, password_hash, "
            "email_2fa_enabled, program_owner, created_at, password_changed_at) "
            "VALUES (?, ?, ?, 0, ?, ?, NULL)",
            (username, email, pw_hash, 1 if program_owner else 0, now),
        )
        c.commit()
        new_id = cur.lastrowid
        row = c.execute("SELECT * FROM users WHERE id=?", (new_id,)).fetchone()
    return dict(row)


def get_user_by_id(user_id: int) -> Optional[dict]:
    with db._get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> Optional[dict]:
    with db._get_conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[dict]:
    with db._get_conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE email=?", (email,)
        ).fetchone()
    return dict(row) if row else None


def verify_password(user_id: int, plaintext: str) -> bool:
    """Constant-time Argon2id verification.

    Returns False if:
      - the user does not exist,
      - the stored hash is malformed,
      - the password does not match,
      - password_changed_at IS NULL (forced first-login change pending).
    """
    user = get_user_by_id(user_id)
    if not user:
        return False
    if user["password_changed_at"] is None:
        return False
    try:
        _hasher.verify(user["password_hash"], plaintext)
        return True
    except (VerifyMismatchError, InvalidHash):
        return False


def update_password(user_id: int, new_plaintext: str) -> None:
    """Set a new password and flip password_changed_at=now()."""
    pw_hash = hash_password(new_plaintext)
    now = _now()
    with db._get_conn() as c:
        c.execute(
            "UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
            (pw_hash, now, user_id),
        )
        c.commit()


def update_last_login_at(user_id: int) -> None:
    now = _now()
    with db._get_conn() as c:
        c.execute(
            "UPDATE users SET last_login_at=? WHERE id=?", (now, user_id)
        )
        c.commit()
