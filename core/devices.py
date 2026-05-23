"""Agent device pairing + revocable token model (backed by state.db).

Pairing codes and device tokens are stored as SHA-256 hashes; the raw values
are returned to the caller exactly once. This mirrors how core.auth stores the
shared password hash.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from core.db import _get_conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_pairing_code(ttl_seconds: int = 600) -> str:
    """Mint a single-use pairing code valid for ttl_seconds."""
    code = secrets.token_urlsafe(9)  # ~12 chars, URL-safe
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_pairing_codes (code_hash, created_at, expires_at, consumed) "
            "VALUES (?, ?, ?, 0)",
            (_hash(code), now.isoformat(),
             (now + timedelta(seconds=ttl_seconds)).isoformat()),
        )
        conn.commit()
    return code


def redeem_pairing_code(
    code: str,
    device_name: str,
    *,
    hwid_hash: str | None = None,
    hostname: str | None = None,
) -> tuple[str, str] | None:
    """Consume a valid code, create a device, return (device_id, raw_token).

    Returns None if the code is unknown, expired, or already consumed.

    *hwid_hash* / *hostname* are optional metadata used by the device
    picker UI. Both nullable to remain backward compatible with older
    agents that don't report them.
    """
    code_hash = _hash(code)
    now = _now()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT expires_at, consumed FROM agent_pairing_codes WHERE code_hash = ?",
            (code_hash,),
        ).fetchone()
        if row is None or row["consumed"]:
            return None
        if datetime.fromisoformat(row["expires_at"]) < now:
            return None
        conn.execute(
            "UPDATE agent_pairing_codes SET consumed = 1 WHERE code_hash = ?",
            (code_hash,),
        )
        device_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO agent_devices (id, name, token_hash, created_at, "
            "last_seen_at, revoked, hwid_hash, hostname) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (device_id, device_name or "device", _hash(token),
             now.isoformat(), now.isoformat(),
             hwid_hash or None, hostname or None),
        )
        conn.commit()
    return device_id, token


def verify_device_token(token: str) -> str | None:
    """Return the device_id for a valid, non-revoked token, else None."""
    if not token:
        return None
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM agent_devices WHERE token_hash = ? AND revoked = 0",
            (_hash(token),),
        ).fetchone()
        return row["id"] if row else None


def touch_device(device_id: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE agent_devices SET last_seen_at = ? WHERE id = ?",
            (_now().isoformat(), device_id),
        )
        conn.commit()


def revoke_device(device_id: str) -> None:
    with _get_conn() as conn:
        conn.execute("UPDATE agent_devices SET revoked = 1 WHERE id = ?", (device_id,))
        conn.commit()


def list_devices() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, last_seen_at, revoked, "
            "hwid_hash, hostname "
            "FROM agent_devices ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def find_by_hwid(hwid_hash: str) -> dict | None:
    """Return the most-recently-paired device row whose hwid_hash matches.

    Used to enable a re-link UX: when an agent reinstalls and pairs again
    on the same machine, we can spot the prior record and offer to merge.
    Returns None if no row matches (including empty/None hwid_hash input).

    Multiple rows can share a hwid_hash (re-pair across reinstalls); we
    return the most-recently-paired one — that's the one currently active
    on the machine and the most useful match for the UI.
    """
    if not hwid_hash:
        return None
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, created_at, last_seen_at, revoked, "
            "hwid_hash, hostname "
            "FROM agent_devices WHERE hwid_hash = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (hwid_hash,),
        ).fetchone()
    return dict(row) if row else None


def get_device_name(device_id: str) -> str | None:
    """Return the human-readable name for *device_id*, or None if not found."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM agent_devices WHERE id = ?", (device_id,)
        ).fetchone()
    return row["name"] if row else None


def most_recently_seen_online(freshness_seconds: int = 60, now: float | None = None) -> dict | None:
    """Return the device dict whose last_seen_at is the largest among
    non-revoked devices, provided it is within freshness_seconds of now.
    Returns None if no device qualifies.

    last_seen_at is stored as ISO-8601 UTC strings; the cutoff is converted
    to the same format so string comparison is lexicographically correct.
    """
    cutoff_ts = (now if now is not None else _now().timestamp()) - freshness_seconds
    cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_devices WHERE revoked = 0 AND last_seen_at >= ? "
            "ORDER BY last_seen_at DESC LIMIT 1",
            (cutoff_iso,),
        ).fetchone()
    return dict(row) if row else None
