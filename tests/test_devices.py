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
