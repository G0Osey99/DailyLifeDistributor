"""Agent-path uploads run on the user's own machine — no web RunLock.

After /media/batch/run?path=agent dispatches to the relay, the web-side
per-user lock is released so the same user can immediately start another
agent-path run, and (more importantly for multi-tenant) the lock dict
doesn't accumulate ghost holders for users whose agents are doing all
the actual work elsewhere.
"""
from __future__ import annotations

import os
import secrets


def _make_user(app, suffix=""):
    from core import db, user_store
    with db._get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO organizations "
            "(id, name, slug, plan, created_at) "
            "VALUES (1, 'O1', 'o1', 'free', datetime('now'))",
        )
        c.commit()
    tag = f"agp{suffix}_{secrets.token_hex(4)}"
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


def test_agent_path_dispatch_releases_per_user_lock(
    app, monkeypatch, tmp_path
):
    """Initialize a run (acquires the per-user lock) and dispatch with
    ?path=agent. After the dispatch returns, the per-user lock must be
    released even though the agent is offline (NoAgentOnlineError path
    explicitly calls _release_run too)."""
    from blueprints import media as media_bp
    from core import media_session as ms

    monkeypatch.setattr(media_bp, "_run_lock", ms.PerUserRunLock())
    monkeypatch.setattr(media_bp, "_runs", {})
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))
    # Enable the agent-path branch.
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")

    client, uid = _make_user(app)
    r = client.post("/media/run/init", json={})
    assert r.status_code == 200, r.data
    run_id = r.get_json()["run_id"]
    # The lock is now held by this user.
    assert media_bp._run_lock.holder(uid) == run_id

    # Trigger batch_run on the agent path with no online agent → the
    # blueprint returns 409 + releases the run lock.
    r2 = client.post(
        f"/media/batch/run?path=agent&run_id={run_id}",
        json={"run_id": run_id, "dates": [], "platforms": [], "files": {}},
    )
    assert r2.status_code in (200, 409), r2.data
    # The key invariant: the per-user lock is released regardless of the
    # dispatch outcome (no_agent_online path also calls _release_run).
    assert media_bp._run_lock.holder(uid) is None


def test_two_agent_users_no_cross_block(app, monkeypatch, tmp_path):
    """Two users on agent path — neither blocks the other."""
    from blueprints import media as media_bp
    from core import media_session as ms

    monkeypatch.setattr(media_bp, "_run_lock", ms.PerUserRunLock())
    monkeypatch.setattr(media_bp, "_runs", {})
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")

    ca, _ = _make_user(app, suffix="_a")
    cb, _ = _make_user(app, suffix="_b")
    ra = ca.post("/media/run/init", json={})
    rb = cb.post("/media/run/init", json={})
    assert ra.status_code == 200
    assert rb.status_code == 200
