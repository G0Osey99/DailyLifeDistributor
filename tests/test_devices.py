import time
from core import db, devices


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    # Re-point the module-level path used by both modules.
    import importlib
    importlib.reload(db)
    importlib.reload(devices)
    db.init_db()


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
