"""History + calendar reads scope to the effective org.

Before this change, ``/history`` listed every session in the DB and
``/calendar`` rendered every upload_history row in the window — across
all tenants. Under impersonation neither swapped scope.
"""
from __future__ import annotations

import pytest

from core import db as _db, org_store, user_store


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SECRET_ENC_KEY", "QcOzn6q5y8yp7v4OoH5sNcWZS_VqIyqU0o8jOwHjW6w=")
    monkeypatch.setenv("FLASK_SECRET_KEY", "t")
    import importlib
    import core.db, core.org_store, core.user_store, core.secrets_store
    importlib.reload(core.db); importlib.reload(core.org_store)
    importlib.reload(core.user_store); importlib.reload(core.secrets_store)
    core.db.init_db()
    import app as m; importlib.reload(m)
    return m.app


def _login_as(client, user_id, org_id, *, acting_as_org_id=None):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["current_org_id"] = org_id
        sess["permission_2fa_passed"] = True
        if acting_as_org_id is not None:
            sess["acting_as_org_id"] = acting_as_org_id


def _seed_session(session_id, label, org_id):
    _db.save_session(session_id, label, "{}", "completed", org_id=org_id)


def _seed_upload(session_id, iso_date, platform, title, org_id):
    """Record an upload_history row stamped with org_id."""
    from datetime import datetime, timezone
    with _db._get_conn() as c:
        c.execute(
            "INSERT INTO upload_history "
            "(session_id, uploaded_at, iso_date, platform, title, file_path, "
            " success, url, scheduled_time, error, org_id) "
            "VALUES (?, ?, ?, ?, ?, '', 1, '', '', '', ?)",
            (session_id, datetime.now(timezone.utc).isoformat(),
             iso_date, platform, title, org_id),
        )
        c.commit()


# ── list_sessions / get_history ───────────────────────────────────────


def test_list_sessions_scoped_by_org(app):
    org_a = org_store.create_org(name="A", slug="a")
    org_b = org_store.create_org(name="B", slug="b")
    _seed_session("s-a", "A run", org_a["id"])
    _seed_session("s-b", "B run", org_b["id"])
    a_sessions = _db.list_sessions(org_id=org_a["id"])
    b_sessions = _db.list_sessions(org_id=org_b["id"])
    a_ids = {s["id"] for s in a_sessions}
    b_ids = {s["id"] for s in b_sessions}
    assert "s-a" in a_ids and "s-b" not in a_ids
    assert "s-b" in b_ids and "s-a" not in b_ids


def test_list_sessions_none_org_returns_all(app):
    """org_id=None preserves the legacy single-tenant / admin behavior."""
    org_a = org_store.create_org(name="A", slug="a")
    org_b = org_store.create_org(name="B", slug="b")
    _seed_session("s-a", "A", org_a["id"])
    _seed_session("s-b", "B", org_b["id"])
    rows = _db.list_sessions()
    assert {"s-a", "s-b"}.issubset({s["id"] for s in rows})


def test_get_history_window_scoped_by_org(app):
    org_a = org_store.create_org(name="A", slug="a")
    org_b = org_store.create_org(name="B", slug="b")
    _seed_session("s-a", "A", org_a["id"])
    _seed_session("s-b", "B", org_b["id"])
    _seed_upload("s-a", "2026-05-10", "YouTube Video", "A vid", org_a["id"])
    _seed_upload("s-b", "2026-05-12", "YouTube Video", "B vid", org_b["id"])
    rows_a = _db.get_history_for_window("2026-05-01", "2026-05-31", org_id=org_a["id"])
    rows_b = _db.get_history_for_window("2026-05-01", "2026-05-31", org_id=org_b["id"])
    titles_a = {r["title"] for r in rows_a}
    titles_b = {r["title"] for r in rows_b}
    assert titles_a == {"A vid"}
    assert titles_b == {"B vid"}


def test_get_history_for_sessions_groups_and_scopes(app):
    """PERF-002: one IN-query returns rows for several sessions, org-scoped,
    so /history can group in Python instead of issuing a per-session query."""
    org_a = org_store.create_org(name="A", slug="a")
    org_b = org_store.create_org(name="B", slug="b")
    _seed_session("s-a1", "A1", org_a["id"])
    _seed_session("s-a2", "A2", org_a["id"])
    _seed_session("s-b1", "B1", org_b["id"])
    _seed_upload("s-a1", "2026-05-10", "YouTube Video", "A1 vid", org_a["id"])
    _seed_upload("s-a1", "2026-05-10", "SimpleCast", "A1 pod", org_a["id"])
    _seed_upload("s-a2", "2026-05-11", "YouTube Video", "A2 vid", org_a["id"])
    _seed_upload("s-b1", "2026-05-12", "YouTube Video", "B1 vid", org_b["id"])

    rows = _db.get_history_for_sessions(["s-a1", "s-a2"], org_id=org_a["id"])
    by_session: dict = {}
    for r in rows:
        by_session.setdefault(r["session_id"], []).append(r)
    assert set(by_session) == {"s-a1", "s-a2"}
    assert len(by_session["s-a1"]) == 2
    assert len(by_session["s-a2"]) == 1
    # org B's row must not leak in even if its id were requested.
    leaked = _db.get_history_for_sessions(["s-b1"], org_id=org_a["id"])
    assert leaked == []
    # Empty input → no query, empty list.
    assert _db.get_history_for_sessions([], org_id=org_a["id"]) == []


def test_pre_migration_null_org_rows_visible_to_each_tenant(app):
    """Rows with org_id IS NULL (pre-migration data) must surface to any
    org's view so the migration cutover doesn't make historical data
    look deleted."""
    org_a = org_store.create_org(name="A", slug="a")
    # Insert a NULL-org legacy row.
    _seed_session("legacy", "Legacy run", None)
    _seed_upload("legacy", "2026-05-10", "YouTube Video", "Legacy vid", None)
    rows = _db.get_history_for_window("2026-05-01", "2026-05-31", org_id=org_a["id"])
    assert any(r["title"] == "Legacy vid" for r in rows)
    sessions = _db.list_sessions(org_id=org_a["id"])
    assert any(s["id"] == "legacy" for s in sessions)


# ── /history route ────────────────────────────────────────────────────


def test_history_route_scopes_to_active_org(app):
    org_a = org_store.create_org(name="A", slug="a")
    org_b = org_store.create_org(name="B", slug="b")
    _seed_session("s-a", "A run", org_a["id"])
    _seed_session("s-b", "B run", org_b["id"])
    user = user_store.create_user(username="u", email="u@x", password="pw1234567")
    org_store.add_membership(user_id=user["id"], org_id=org_a["id"], role="owner")
    client = app.test_client()
    _login_as(client, user["id"], org_a["id"])
    body = client.get("/history").data
    assert b"A run" in body
    assert b"B run" not in body, "history view leaked another org's session"


def test_history_swaps_under_impersonation(app):
    boot = org_store.create_org(name="LCBC", slug="lcbc")
    target = org_store.create_org(name="Tgt", slug="tgt")
    _seed_session("s-boot", "LCBC session", boot["id"])
    _seed_session("s-tgt", "TGT session", target["id"])
    po = user_store.create_user(
        username="po", email="po@x", password="pw1234567", program_owner=True,
    )
    org_store.add_membership(user_id=po["id"], org_id=boot["id"], role="owner")
    client = app.test_client()
    _login_as(client, po["id"], boot["id"], acting_as_org_id=target["id"])
    body = client.get("/history").data
    assert b"TGT session" in body
    assert b"LCBC session" not in body, (
        "history under impersonation should show target's runs, not bootstrap's"
    )


# ── impersonation mirror to users.acting_as_org_id ────────────────────


def test_impersonation_mirrors_to_users_row(app):
    boot = org_store.create_org(name="LCBC", slug="lcbc")
    target = org_store.create_org(name="Tgt", slug="tgt")
    po = user_store.create_user(
        username="po", email="po@x", password="pw1234567", program_owner=True,
    )
    org_store.add_membership(user_id=po["id"], org_id=boot["id"], role="owner")
    client = app.test_client()
    _login_as(client, po["id"], boot["id"])
    client.post(f"/admin/organizations/{target['id']}/impersonate",
                follow_redirects=False)
    with _db._get_conn() as c:
        row = c.execute(
            "SELECT acting_as_org_id FROM users WHERE id = ?", (po["id"],),
        ).fetchone()
    assert row["acting_as_org_id"] == target["id"]
    client.post("/admin/exit-impersonation", follow_redirects=False)
    with _db._get_conn() as c:
        row = c.execute(
            "SELECT acting_as_org_id FROM users WHERE id = ?", (po["id"],),
        ).fetchone()
    assert row["acting_as_org_id"] is None
