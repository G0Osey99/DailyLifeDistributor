"""Phase δ disk-budget admission control.

If the VPS temp volume has less than DLD_DISK_MIN_FREE_BYTES free (default
5 GiB), /media/run/init returns 507 with a "use the agent path" message.
Setting the env var to 0 disables the floor.
"""
from __future__ import annotations

import shutil
from collections import namedtuple


from core import media_session as ms


_FakeUsage = namedtuple("_FakeUsage", "total used free")


def test_has_minimum_free_space_uses_default_5gib(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path))
    # 6 GiB free → over the 5 GiB default floor.
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: _FakeUsage(total=10 * 2**30, used=4 * 2**30,
                              free=6 * 2**30),
    )
    monkeypatch.delenv("DLD_DISK_MIN_FREE_BYTES", raising=False)
    assert ms.has_minimum_free_space() is True


def test_has_minimum_free_space_refuses_when_below_floor(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path))
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: _FakeUsage(total=10 * 2**30, used=9 * 2**30,
                              free=1 * 2**30),
    )
    monkeypatch.delenv("DLD_DISK_MIN_FREE_BYTES", raising=False)
    assert ms.has_minimum_free_space() is False


def test_floor_zero_disables_admission(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path))
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: _FakeUsage(total=10, used=10, free=0),
    )
    monkeypatch.setenv("DLD_DISK_MIN_FREE_BYTES", "0")
    assert ms.has_minimum_free_space() is True


def test_floor_overridable_via_env(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path))
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: _FakeUsage(total=10 * 2**30, used=8 * 2**30,
                              free=2 * 2**30),
    )
    # 2 GiB free is under default 5 GiB but over a 1 GiB override.
    monkeypatch.setenv("DLD_DISK_MIN_FREE_BYTES", str(1 * 2**30))
    assert ms.has_minimum_free_space() is True
    monkeypatch.setenv("DLD_DISK_MIN_FREE_BYTES", str(3 * 2**30))
    assert ms.has_minimum_free_space() is False


def _login_user(app, oid=1, suffix=""):
    from core import db, user_store
    with db._get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO organizations "
            "(id, name, slug, plan, created_at) "
            "VALUES (?, ?, ?, 'free', datetime('now'))",
            (oid, f"Org {oid}", f"org-{oid}"),
        )
        c.commit()
    tag = f"u{suffix}_o{oid}"
    user = user_store.create_user(
        username=tag, email=f"{tag}@example.com",
        password="long-enough-pw-12!",
    )
    with db._get_conn() as c:
        c.execute(
            "INSERT INTO org_memberships "
            "(user_id, org_id, role, joined_at) "
            "VALUES (?, ?, 'user', datetime('now'))",
            (user["id"], oid),
        )
        c.commit()
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = user["id"]
        s["current_org_id"] = oid
    return client


def test_run_init_507s_when_disk_below_floor(app, monkeypatch, tmp_path):
    """Login + POST /media/run/init when disk is below the floor → 507."""
    from blueprints import media as media_bp
    monkeypatch.setattr(media_bp, "_run_lock", ms.PerUserRunLock())
    monkeypatch.setattr(media_bp, "_runs", {})
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: _FakeUsage(total=10 * 2**30, used=9 * 2**30,
                              free=1 * 2**30),
    )
    monkeypatch.delenv("DLD_DISK_MIN_FREE_BYTES", raising=False)
    client = _login_user(app)
    r = client.post("/media/run/init", json={})
    assert r.status_code == 507
    assert b"agent path" in r.data or b"storage full" in r.data
