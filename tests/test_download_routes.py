"""/download/agent landing + per-OS redirects (phase δ)."""
from __future__ import annotations


def _login(app):
    from core import db, user_store
    with db._get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO organizations "
            "(id, name, slug, plan, created_at) "
            "VALUES (1, 'O1', 'o1', 'free', datetime('now'))",
        )
        c.commit()
    user = user_store.create_user(
        username="dl_u", email="dl@example.com",
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
    return client


def test_landing_page_renders_for_authed_user(app):
    client = _login(app)
    r = client.get("/download/agent")
    assert r.status_code == 200
    body = r.data.lower()
    assert b"windows" in body and b"macos" in body


def test_landing_redirects_unauth_to_login(app):
    client = app.test_client()
    r = client.get("/download/agent", follow_redirects=False)
    assert r.status_code in (302, 401)


def test_windows_redirect(app):
    client = _login(app)
    r = client.get("/download/agent/windows", follow_redirects=False)
    assert r.status_code == 302
    assert "/agent/releases/" in r.headers.get("Location", "")


def test_macos_redirect(app):
    client = _login(app)
    r = client.get("/download/agent/macos", follow_redirects=False)
    assert r.status_code == 302
    assert "/agent/releases/" in r.headers.get("Location", "")


def test_landing_highlights_windows_for_windows_ua(app):
    client = _login(app)
    r = client.get(
        "/download/agent",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
        },
    )
    assert r.status_code == 200
    # The page renders an `os-detected` data attribute the frontend uses.
    assert b'data-os="windows"' in r.data or b"detected-windows" in r.data


def test_landing_highlights_macos_for_mac_ua(app):
    client = _login(app)
    r = client.get(
        "/download/agent",
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit",
        },
    )
    assert r.status_code == 200
    assert b'data-os="macos"' in r.data or b"detected-macos" in r.data
