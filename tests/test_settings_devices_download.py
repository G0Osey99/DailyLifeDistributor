"""/settings/devices renders a persistent agent-download section (phase δ).

This section is always visible — for re-installs, adding a second machine,
or just looking up the latest binaries. Independent of how many devices
the user already has.
"""
from __future__ import annotations

import secrets


def _make_user(app):
    from core import db, user_store
    with db._get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO organizations "
            "(id, name, slug, plan, created_at) "
            "VALUES (1, 'O1', 'o1', 'free', datetime('now'))",
        )
        c.commit()
    tag = f"dev{secrets.token_hex(4)}"
    user = user_store.create_user(
        username=tag, email=f"{tag}@example.com",
        password="long-enough-pw-12!",
    )
    with db._get_conn() as c:
        c.execute(
            "INSERT INTO org_memberships "
            "(user_id, org_id, role, joined_at) "
            "VALUES (?, 1, 'user', datetime('now'))",
            (user["id"],),
        )
        c.commit()
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = user["id"]
        s["current_org_id"] = 1
    return client, user["id"]


def test_settings_devices_includes_download_section(app):
    client, _ = _make_user(app)
    r = client.get("/settings/devices")
    assert r.status_code == 200
    # The section is identified by data-test-id="settings-download-section".
    assert b'data-test-id="settings-download-section"' in r.data
    # And links to the /download/agent landing page.
    assert b"/download/agent" in r.data


def test_settings_download_section_visible_with_devices(app):
    """Persistent — visible even when the user already has paired devices."""
    from core import db
    client, uid = _make_user(app)
    with db._get_conn() as c:
        c.execute(
            "INSERT INTO agent_devices "
            "(id, name, token_hash, created_at, last_seen_at, revoked, user_id) "
            "VALUES ('d1', 'n', 't', datetime('now'), datetime('now'), 0, ?)",
            (uid,),
        )
        c.commit()
    r = client.get("/settings/devices")
    assert r.status_code == 200
    assert b'data-test-id="settings-download-section"' in r.data


def test_settings_devices_does_not_leak_other_users_devices(app):
    """SEC-001: /settings/devices must show only the caller's own devices.

    Previously it rendered the system-wide list_devices(), so any
    authenticated user saw every other tenant's device inventory. User B
    (a different, non-program-owner user, even in the same org) must not
    see user A's device."""
    from core import db
    # User A owns a distinctively-named device.
    _client_a, uid_a = _make_user(app)
    with db._get_conn() as c:
        c.execute(
            "INSERT INTO agent_devices "
            "(id, name, token_hash, created_at, last_seen_at, revoked, user_id) "
            "VALUES ('devA', 'STUDIO-LAPTOP-A', 't', datetime('now'), "
            "datetime('now'), 0, ?)",
            (uid_a,),
        )
        c.commit()
    # User B is a different user (same org 1) with no devices of their own.
    client_b, _uid_b = _make_user(app)
    r = client_b.get("/settings/devices")
    assert r.status_code == 200
    assert b"STUDIO-LAPTOP-A" not in r.data, (
        "cross-tenant device leak: user B saw user A's device name"
    )
    assert b"devA" not in r.data
