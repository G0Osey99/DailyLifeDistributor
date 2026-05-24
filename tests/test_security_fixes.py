"""Regression tests for the security-audit fixes.

These pin the behaviors we just hardened so future refactors can't
silently regress them. Each test names the original finding it covers.
"""
from __future__ import annotations

import pytest

from core import devices, user_store


@pytest.fixture
def app(monkeypatch):
    """App with HYBRID_AGENT_ENABLED so /agent/devices/* routes are
    registered. Mirrors the conftest fixture otherwise."""
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    monkeypatch.setenv("RATELIMIT_ENABLED", "false")
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-security-fixes")
    monkeypatch.setenv("HOSTED", "")  # keep the legacy-on-hosted guard quiet
    from app import create_app
    a = create_app()
    a.config["TESTING"] = True
    yield a


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# CRITICAL: email 2FA must not be disable-able without proof of factor.
# ---------------------------------------------------------------------------

def test_email_2fa_disable_requires_code(client, app):
    """POST /settings/2fa/disable method=email — without code, must NOT
    flip the bit. Pre-fix this was a one-POST 2FA strip."""
    from core import db as _db
    u = user_store.create_user(username="alice", email="a@x.com",
                                password="bootstrap1234")
    user_store.update_password(u["id"], "newgoodpw12345")
    _db.set_user_email_2fa(u["id"], True)

    with client.session_transaction() as s:
        s["user_id"] = u["id"]

    # Without a code: refused.
    r = client.post("/settings/2fa/disable", data={"method": "email"})
    assert r.status_code == 400
    fresh = _db.get_user_by_id(u["id"])
    assert fresh["email_2fa_enabled"] == 1, "must NOT have disabled email 2FA"


def test_email_2fa_disable_rejects_wrong_code(client, app):
    """With an invalid code, refuse."""
    from core import db as _db
    u = user_store.create_user(username="bob", email="b@x.com",
                                password="bootstrap1234")
    user_store.update_password(u["id"], "newgoodpw12345")
    _db.set_user_email_2fa(u["id"], True)

    with client.session_transaction() as s:
        s["user_id"] = u["id"]

    r = client.post("/settings/2fa/disable",
                    data={"method": "email", "code": "000000"})
    assert r.status_code == 400
    fresh = _db.get_user_by_id(u["id"])
    assert fresh["email_2fa_enabled"] == 1


# ---------------------------------------------------------------------------
# HIGH: device endpoints must not let user A touch user B's devices.
# ---------------------------------------------------------------------------

def test_device_revoke_blocked_for_non_owner(client, app):
    """User B can't revoke user A's device."""
    from core import db as _db
    # Two users, each with one device.
    u_a = user_store.create_user(username="alice", email="a@x.com",
                                  password="bootstrap1234")
    user_store.update_password(u_a["id"], "newgoodpw12345")
    u_b = user_store.create_user(username="bob", email="b@x.com",
                                  password="bootstrap1234")
    user_store.update_password(u_b["id"], "newgoodpw12345")

    with _db._get_conn() as c:
        c.execute(
            "INSERT INTO agent_devices (id, name, token_hash, created_at, "
            "user_id) VALUES ('dev-a', 'alice-laptop', 'h', "
            "'2026-01-01T00:00:00+00:00', ?)",
            (u_a["id"],),
        )
        c.commit()

    # Sign in as B and try to revoke A's device.
    with client.session_transaction() as s:
        s["user_id"] = u_b["id"]
    r = client.post("/agent/devices/dev-a/revoke")
    assert r.status_code == 404, "B must not see A's device"

    # Confirm the device wasn't actually revoked.
    with _db._get_conn() as c:
        row = c.execute(
            "SELECT revoked FROM agent_devices WHERE id='dev-a'"
        ).fetchone()
    assert row["revoked"] == 0


def test_device_list_scoped_to_user(client, app):
    """GET /agent/devices returns only the caller's devices."""
    from core import db as _db
    u_a = user_store.create_user(username="alice", email="a@x.com",
                                  password="bootstrap1234")
    user_store.update_password(u_a["id"], "newgoodpw12345")
    u_b = user_store.create_user(username="bob", email="b@x.com",
                                  password="bootstrap1234")
    user_store.update_password(u_b["id"], "newgoodpw12345")

    with _db._get_conn() as c:
        c.executemany(
            "INSERT INTO agent_devices (id, name, token_hash, created_at, "
            "user_id) VALUES (?, ?, 'h', '2026-01-01T00:00:00+00:00', ?)",
            [
                ("dev-alice", "alice-laptop", u_a["id"]),
                ("dev-bob",   "bob-laptop",   u_b["id"]),
            ],
        )
        c.commit()

    with client.session_transaction() as s:
        s["user_id"] = u_b["id"]
    r = client.get("/agent/devices")
    assert r.status_code == 200
    ids = {d["id"] for d in r.get_json()["devices"]}
    assert ids == {"dev-bob"}, f"B should only see their device, got {ids}"


# ---------------------------------------------------------------------------
# HIGH: recovery escalation — non-program-owner can't approve recovery for
# a program-owner.
# ---------------------------------------------------------------------------

def test_recovery_approve_blocks_owner_targeting_program_owner(client, app):
    """An ordinary org Owner must NOT be able to approve recovery for a
    program_owner-flagged user (would let them take over the master)."""
    from core import db as _db, org_store
    # Set up an org with a regular Owner (alice) AND the program-owner (admin).
    admin = user_store.create_user(
        username="admin", email="admin@x.com", password="bootstrap1234",
        program_owner=True,
    )
    user_store.update_password(admin["id"], "adminpw1234567")
    alice = user_store.create_user(
        username="alice", email="alice@x.com", password="bootstrap1234",
    )
    user_store.update_password(alice["id"], "alicepw1234567")
    o = org_store.create_org(name="O", slug="o", created_by_user_id=alice["id"])
    org_store.add_membership(user_id=alice["id"], org_id=o["id"], role="owner")
    org_store.add_membership(user_id=admin["id"], org_id=o["id"], role="owner")

    # Alice files a recovery request *for admin* — direct DB insert (the
    # /recover form is rate-limited to one per 24h per user).
    import hashlib
    with _db._get_conn() as c:
        c.execute(
            "INSERT INTO recovery_requests (user_id, requested_at, expires_at) "
            "VALUES (?, '2026-01-01T00:00:00+00:00', '2026-12-31T00:00:00+00:00')",
            (admin["id"],),
        )
        rid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        c.commit()

    # Sign in as Alice and try to approve.
    with client.session_transaction() as s:
        s["user_id"] = alice["id"]
    r = client.get(f"/admin-actions/recovery/{rid}/approve")
    assert r.status_code == 403, \
        "Non-program-owner must not approve recovery for program-owner"

    # The request must still be unapproved.
    with _db._get_conn() as c:
        row = c.execute(
            "SELECT approved_at FROM recovery_requests WHERE id=?", (rid,),
        ).fetchone()
    assert row["approved_at"] is None
