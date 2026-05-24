"""Empty-state agent-download card on the root dashboard (phase δ).

When the current user has zero paired devices, show the card; otherwise
hide it. count_user_devices is the new core.devices helper.
"""
from __future__ import annotations

import secrets


def _make_user(app, *, suffix=""):
    from core import db, user_store
    with db._get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO organizations "
            "(id, name, slug, plan, created_at) "
            "VALUES (1, 'O1', 'o1', 'free', datetime('now'))",
        )
        c.commit()
    tag = f"dash{suffix}_{secrets.token_hex(4)}"
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


def _create_device(user_id):
    """Insert a fake non-revoked device for the user."""
    from core import db
    with db._get_conn() as c:
        c.execute(
            "INSERT INTO agent_devices "
            "(id, name, token_hash, created_at, last_seen_at, "
            " revoked, user_id) "
            "VALUES (?, 'dev', 'token', datetime('now'), datetime('now'), "
            " 0, ?)",
            (f"dev-{secrets.token_hex(4)}", user_id),
        )
        c.commit()


def test_count_user_devices_returns_zero_for_new_user(app):
    from core import devices
    _, uid = _make_user(app)
    assert devices.count_user_devices(uid) == 0


def test_count_user_devices_skips_revoked(app):
    from core import db, devices
    _, uid = _make_user(app)
    _create_device(uid)
    with db._get_conn() as c:
        c.execute(
            "INSERT INTO agent_devices "
            "(id, name, token_hash, created_at, last_seen_at, "
            " revoked, user_id) "
            "VALUES ('rev', 'r', 't', datetime('now'), datetime('now'), 1, ?)",
            (uid,),
        )
        c.commit()
    assert devices.count_user_devices(uid) == 1


def test_dashboard_renders_download_card_for_user_with_no_devices(app):
    client, _ = _make_user(app)
    r = client.get("/dashboard")
    assert r.status_code == 200
    body = r.data.lower()
    # Card markup is identified by the data-test-id attribute.
    assert b'data-test-id="empty-state-download-card"' in r.data


def test_dashboard_hides_download_card_when_user_has_devices(app):
    client, uid = _make_user(app)
    _create_device(uid)
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert b'data-test-id="empty-state-download-card"' not in r.data
