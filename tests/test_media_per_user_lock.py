"""/media/run/init takes the per-user lock — two users can init in parallel."""
from __future__ import annotations


def _login_user(app, *, oid: int = 1, role: str = "user", suffix: str = ""):
    """Mint a fresh user + membership + test_client with session primed."""
    from core import db, user_store
    with db._get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO organizations "
            "(id, name, slug, plan, created_at) "
            "VALUES (?, ?, ?, 'free', datetime('now'))",
            (oid, f"Org {oid}", f"org-{oid}"),
        )
        c.commit()
    tag = f"u{role}{suffix}_o{oid}"
    user = user_store.create_user(
        username=tag, email=f"{tag}@example.com",
        password="long-enough-pw-12!",
    )
    with db._get_conn() as c:
        c.execute(
            "INSERT INTO org_memberships "
            "(user_id, org_id, role, joined_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (user["id"], oid, role),
        )
        c.commit()
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = user["id"]
        s["current_org_id"] = oid
    return client, user["id"]


def test_two_users_can_init_runs_concurrently(app, monkeypatch, tmp_path):
    """Two different users hitting /media/run/init both succeed."""
    from blueprints import media as media_bp
    from core import media_session as ms
    # Fresh lock + temp root for the test
    monkeypatch.setattr(media_bp, "_run_lock", ms.PerUserRunLock())
    monkeypatch.setattr(media_bp, "_runs", {})
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))

    client_a, _ = _login_user(app, suffix="_a")
    client_b, _ = _login_user(app, suffix="_b")

    r1 = client_a.post("/media/run/init", json={})
    assert r1.status_code == 200, r1.data
    r2 = client_b.post("/media/run/init", json={})
    assert r2.status_code == 200, r2.data


def test_same_user_second_init_is_409(app, monkeypatch, tmp_path):
    from blueprints import media as media_bp
    from core import media_session as ms
    monkeypatch.setattr(media_bp, "_run_lock", ms.PerUserRunLock())
    monkeypatch.setattr(media_bp, "_runs", {})
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))

    client, _ = _login_user(app)
    r1 = client.post("/media/run/init", json={})
    assert r1.status_code == 200
    r2 = client.post("/media/run/init", json={})
    assert r2.status_code == 409
