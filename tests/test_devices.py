import time
from datetime import datetime, timezone
from core import db, devices


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    # Re-point the module-level path used by both modules.
    import importlib
    importlib.reload(db)
    importlib.reload(devices)
    db.init_db()


def _set_last_seen(device_id: str, ts: float) -> None:
    """Directly write a specific last_seen_at (unix epoch) for a device."""
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    with db._get_conn() as conn:
        conn.execute(
            "UPDATE agent_devices SET last_seen_at = ? WHERE id = ?",
            (iso, device_id),
        )
        conn.commit()


def test_redeem_valid_code_creates_device(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code(ttl_seconds=300)
    assert isinstance(code, str) and len(code) >= 8
    device_id, token = devices.redeem_pairing_code(code, "Ryker-Mac")
    assert device_id and token
    assert devices.verify_device_token(token) == device_id


def test_code_is_single_use(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    devices.redeem_pairing_code(code, "dev1")
    assert devices.redeem_pairing_code(code, "dev2") is None


def test_expired_code_rejected(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code(ttl_seconds=0)
    time.sleep(0.01)
    assert devices.redeem_pairing_code(code, "dev1") is None


def test_revoked_token_fails_verify(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    device_id, token = devices.redeem_pairing_code(code, "dev1")
    devices.revoke_device(device_id)
    assert devices.verify_device_token(token) is None


def test_list_devices_reports_revoked(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    device_id, _ = devices.redeem_pairing_code(code, "dev1")
    devices.revoke_device(device_id)
    rows = devices.list_devices()
    assert any(r["id"] == device_id and r["revoked"] == 1 for r in rows)


def test_most_recently_seen_online_picks_highest_last_seen(tmp_path, monkeypatch):
    # Two devices, both online (last_seen within freshness window),
    # the one with the later last_seen wins.
    _fresh_db(tmp_path, monkeypatch)
    code_a = devices.create_pairing_code()
    dev_a_id, _ = devices.redeem_pairing_code(code_a, "dev-a")
    _set_last_seen(dev_a_id, ts=100)

    code_b = devices.create_pairing_code()
    dev_b_id, _ = devices.redeem_pairing_code(code_b, "dev-b")
    _set_last_seen(dev_b_id, ts=200)

    result = devices.most_recently_seen_online(freshness_seconds=300, now=250)
    assert result is not None
    assert result["name"] == "dev-b"


def test_most_recently_seen_online_returns_none_when_all_stale(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    code_a = devices.create_pairing_code()
    dev_a_id, _ = devices.redeem_pairing_code(code_a, "dev-a")
    _set_last_seen(dev_a_id, ts=10)

    assert devices.most_recently_seen_online(freshness_seconds=60, now=1000) is None


# ---------------------------------------------------------------------------
# Phase 3.5 — HWID + hostname persistence + find_by_hwid
# ---------------------------------------------------------------------------

def test_create_persists_hwid_and_hostname(tmp_path, monkeypatch):
    """redeem_pairing_code with hwid_hash + hostname must persist both."""
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    h = "a" * 64
    device_id, _ = devices.redeem_pairing_code(
        code, "Ryker-Mac",
        hwid_hash=h,
        hostname="Studio",
    )
    rows = devices.list_devices()
    row = next(r for r in rows if r["id"] == device_id)
    assert row["hwid_hash"] == h
    assert row["hostname"] == "Studio"


def test_create_without_hwid_and_hostname_stores_null(tmp_path, monkeypatch):
    """Backward-compat: older agents that don't send the fields still pair,
    and the row stores NULL for hwid_hash + hostname."""
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    device_id, _ = devices.redeem_pairing_code(code, "Ryker-Mac")
    row = next(r for r in devices.list_devices() if r["id"] == device_id)
    assert row["hwid_hash"] is None
    assert row["hostname"] is None


def test_find_by_hwid_returns_device(tmp_path, monkeypatch):
    """find_by_hwid returns the row whose hwid_hash matches."""
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    h = "b" * 64
    device_id, _ = devices.redeem_pairing_code(
        code, "Mac", hwid_hash=h, hostname="Studio")
    row = devices.find_by_hwid(h)
    assert row is not None
    assert row["id"] == device_id
    assert row["hostname"] == "Studio"


def test_find_by_hwid_returns_none_for_missing(tmp_path, monkeypatch):
    """find_by_hwid returns None when no row matches."""
    _fresh_db(tmp_path, monkeypatch)
    assert devices.find_by_hwid("z" * 64) is None


def test_find_by_hwid_returns_none_for_empty(tmp_path, monkeypatch):
    """Defensive: empty / None inputs return None without querying."""
    _fresh_db(tmp_path, monkeypatch)
    assert devices.find_by_hwid("") is None
    assert devices.find_by_hwid(None) is None  # type: ignore[arg-type]


def test_find_by_hwid_picks_most_recent_on_collision(tmp_path, monkeypatch):
    """When two rows share an hwid_hash (re-pair), return the newest."""
    _fresh_db(tmp_path, monkeypatch)
    h = "c" * 64
    # First pairing.
    code_a = devices.create_pairing_code()
    old_id, _ = devices.redeem_pairing_code(
        code_a, "Mac", hwid_hash=h, hostname="Studio-old")
    # Force a back-dated created_at on the old row so the new one wins.
    import time
    time.sleep(0.01)
    code_b = devices.create_pairing_code()
    new_id, _ = devices.redeem_pairing_code(
        code_b, "Mac", hwid_hash=h, hostname="Studio-new")

    row = devices.find_by_hwid(h)
    assert row is not None
    # The most-recent pairing wins.
    assert row["id"] == new_id
    assert row["hostname"] == "Studio-new"


def test_list_devices_includes_hwid_and_hostname(tmp_path, monkeypatch):
    """list_devices() exposes hwid_hash + hostname on every row."""
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    devices.redeem_pairing_code(
        code, "Mac", hwid_hash="d" * 64, hostname="Studio")
    rows = devices.list_devices()
    assert len(rows) == 1
    assert "hwid_hash" in rows[0]
    assert "hostname" in rows[0]


def test_migration_idempotent_adds_columns_to_legacy_db(tmp_path, monkeypatch):
    """init_db() must idempotently add hwid_hash + hostname to a pre-existing
    agent_devices table without those columns. Re-running init_db() is a
    no-op (no error)."""
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    import importlib
    importlib.reload(db)
    importlib.reload(devices)

    # Build the legacy schema (no hwid_hash / hostname).
    with db._get_conn() as conn:
        conn.execute("""
            CREATE TABLE agent_devices (
                id TEXT PRIMARY KEY,
                name TEXT,
                token_hash TEXT NOT NULL,
                created_at TEXT,
                last_seen_at TEXT,
                revoked INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Insert a legacy row.
        conn.execute(
            "INSERT INTO agent_devices (id, name, token_hash, created_at, "
            "last_seen_at, revoked) VALUES (?, ?, ?, ?, ?, 0)",
            ("legacy-id", "Legacy", "hash", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()

    # First migration — must add the columns.
    db.init_db()
    with db._get_conn() as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info('agent_devices')").fetchall()}
    assert "hwid_hash" in cols
    assert "hostname" in cols

    # Legacy row survives, with NULLs in the new columns.
    rows = devices.list_devices()
    legacy = next(r for r in rows if r["id"] == "legacy-id")
    assert legacy["hwid_hash"] is None
    assert legacy["hostname"] is None

    # Second run — idempotent, no error.
    db.init_db()
    db.init_db()


def test_most_recently_seen_online_includes_hwid_and_hostname(tmp_path, monkeypatch):
    """The dict returned by most_recently_seen_online must include the
    new HWID + hostname fields (it SELECT *'s, so any agent_devices column
    is exposed)."""
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    device_id, _ = devices.redeem_pairing_code(
        code, "Mac", hwid_hash="e" * 64, hostname="Studio")
    _set_last_seen(device_id, ts=200)

    row = devices.most_recently_seen_online(freshness_seconds=300, now=250)
    assert row is not None
    assert row["hwid_hash"] == "e" * 64
    assert row["hostname"] == "Studio"


# ---------------------------------------------------------------------------
# set_device_name (rename UX)
# ---------------------------------------------------------------------------
def test_set_device_name_updates_row(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    device_id, _ = devices.redeem_pairing_code(
        code, "old-name", hwid_hash="a" * 64, hostname="Mac.local")
    assert devices.set_device_name(device_id, "Studio Mac") is True
    rows = devices.list_devices()
    row = next(r for r in rows if r["id"] == device_id)
    assert row["name"] == "Studio Mac"
    # Hostname must remain untouched — it's the agent-reported system
    # name, not user-editable.
    assert row["hostname"] == "Mac.local"


def test_set_device_name_trims_whitespace(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    device_id, _ = devices.redeem_pairing_code(code, "old")
    assert devices.set_device_name(device_id, "  Padded Name  ") is True
    assert devices.get_device_name(device_id) == "Padded Name"


def test_set_device_name_missing_returns_false(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    assert devices.set_device_name("nope", "anything") is False


def test_set_device_name_revoked_returns_false(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    device_id, _ = devices.redeem_pairing_code(code, "x")
    devices.revoke_device(device_id)
    # Revoked rows are read-only from the rename API's perspective —
    # users can still see them but can't edit them in place.
    assert devices.set_device_name(device_id, "new name") is False


def test_set_device_name_empty_raises(tmp_path, monkeypatch):
    import pytest as _pt
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    device_id, _ = devices.redeem_pairing_code(code, "x")
    with _pt.raises(devices.DeviceNameEmpty):
        devices.set_device_name(device_id, "")
    with _pt.raises(devices.DeviceNameEmpty):
        devices.set_device_name(device_id, "   ")


def test_set_device_name_too_long_raises(tmp_path, monkeypatch):
    import pytest as _pt
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    device_id, _ = devices.redeem_pairing_code(code, "x")
    with _pt.raises(devices.DeviceNameTooLong):
        devices.set_device_name(device_id, "a" * (devices.DEVICE_NAME_MAX_LEN + 1))
    # Exactly at the limit must succeed.
    assert devices.set_device_name(device_id, "a" * devices.DEVICE_NAME_MAX_LEN) is True
