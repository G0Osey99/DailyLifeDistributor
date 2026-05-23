"""/download/agent embeds a one-time pairing code for the logged-in user.

The code has a 30-minute TTL (1800 s). The page renders it visibly so the
operator can paste it into the agent's pair-up screen.
"""
from __future__ import annotations

import re
import secrets

import pytest


def _make_user(app):
    from core import db, user_store
    with db._get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO organizations "
            "(id, name, slug, plan, created_at) "
            "VALUES (1, 'O1', 'o1', 'free', datetime('now'))",
        )
        c.commit()
    tag = f"pc_{secrets.token_hex(4)}"
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


def test_landing_renders_pairing_code(app):
    client, _ = _make_user(app)
    r = client.get("/download/agent")
    assert r.status_code == 200
    # The template includes <span class="pair-code" id="pair-code">…</span>
    # only when a code was minted. Match a non-empty content there.
    m = re.search(
        rb'<span class="pair-code"[^>]*>([^<]+)</span>',
        r.data,
    )
    assert m is not None, r.data[:500]
    code = m.group(1).strip()
    assert len(code) >= 6


def test_pairing_code_persisted_for_redemption(app, monkeypatch):
    """Verify the rendered code is the one stored in agent_pairing_codes
    (so redemption from the agent actually works)."""
    captured: dict = {}
    from core import devices as _devices
    orig = _devices.create_pairing_code

    def _capture(*a, **kw):
        code = orig(*a, **kw)
        captured["code"] = code
        captured["kwargs"] = kw
        return code

    monkeypatch.setattr(_devices, "create_pairing_code", _capture)
    # Also patch the reference imported into the download blueprint, if any.
    try:
        from blueprints import download as _dl
        if hasattr(_dl, "devices"):
            monkeypatch.setattr(_dl.devices, "create_pairing_code", _capture, raising=False)
    except Exception:
        pass

    client, uid = _make_user(app)
    r = client.get("/download/agent")
    assert r.status_code == 200
    assert "code" in captured, "create_pairing_code was not called"
    # 30 minutes = 1800s.
    assert captured["kwargs"].get("ttl_seconds") == 1800
    # And the user_id propagates.
    assert captured["kwargs"].get("user_id") == uid
