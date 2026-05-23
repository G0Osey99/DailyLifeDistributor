"""user_id propagates from pairing-code creation to the device row.

Multi-tenant phase β: the agent CLI POSTs /agent/pair/redeem without a
session, so the redeem side cannot read user_id from Flask state. We
record the requesting user_id at create_pairing_code time (session-gated)
and propagate it onto the new device record on redeem.
"""
from __future__ import annotations

from core import db, devices


def test_device_inherits_user_id_from_code_creation():
    # Seed an org + user so the FK passes (the column itself is nullable
    # so the test does not strictly need a row, but having one keeps the
    # production semantics realistic).
    with db._get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO users (id, username, email, password_hash, created_at) "
            "VALUES (42, 'u42', 'u42@x', 'x', datetime('now'))"
        )
        c.commit()
    code = devices.create_pairing_code(user_id=42)
    result = devices.redeem_pairing_code(code, "laptop")
    assert result is not None
    device_id, _token = result
    with db._get_conn() as c:
        row = c.execute(
            "SELECT user_id FROM agent_devices WHERE id = ?",
            (device_id,),
        ).fetchone()
    assert row["user_id"] == 42


def test_legacy_pair_records_null_user_id():
    """A pairing code generated without a session (legacy single-tenant)
    yields a device row with NULL user_id."""
    code = devices.create_pairing_code()
    result = devices.redeem_pairing_code(code, "legacy-laptop")
    assert result is not None
    device_id, _ = result
    with db._get_conn() as c:
        row = c.execute(
            "SELECT user_id FROM agent_devices WHERE id = ?",
            (device_id,),
        ).fetchone()
    assert row["user_id"] is None
