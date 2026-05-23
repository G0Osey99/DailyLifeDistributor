"""HTTP tests for the device-management UI + rename endpoint.

Covers:
  - POST /agent/devices/<id>/name — happy path + validation + 404/410
  - GET /settings/devices — renders without HYBRID + with HYBRID
"""
import pytest
from core import auth


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    import importlib
    import core.db as db
    import core.devices as devices
    importlib.reload(db)
    importlib.reload(devices)
    db.init_db()
    auth.reset_lockouts()
    auth.set_password("correct-horse")
    import app as flask_app_module
    importlib.reload(flask_app_module)
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def _login(c):
    c.post("/login", data={"password": "correct-horse"})


def _redeem(client, name="Mac"):
    _login(client)
    code = client.post("/agent/pair/new").get_json()["code"]
    other = client.application.test_client()
    other.post("/agent/pair/redeem", json={"code": code, "name": name})
    devs = client.get("/agent/devices").get_json()["devices"]
    return devs[0]["id"]


# ---------------------------------------------------------------------------
# POST /agent/devices/<id>/name
# ---------------------------------------------------------------------------
def test_rename_requires_auth(client):
    other = client.application.test_client()
    # No login on `other`.
    resp = other.post("/agent/devices/x/name", json={"name": "new"})
    assert resp.status_code in (302, 401)


def test_rename_happy_path(client):
    did = _redeem(client, name="OldName")
    resp = client.post(
        f"/agent/devices/{did}/name", json={"name": "Studio Mac"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["device"]["name"] == "Studio Mac"


def test_rename_empty_returns_400(client):
    did = _redeem(client)
    resp = client.post(f"/agent/devices/{did}/name", json={"name": ""})
    assert resp.status_code == 400
    assert "empty" in resp.get_json()["error"].lower()


def test_rename_whitespace_only_returns_400(client):
    did = _redeem(client)
    resp = client.post(f"/agent/devices/{did}/name", json={"name": "    "})
    assert resp.status_code == 400


def test_rename_too_long_returns_400(client):
    did = _redeem(client)
    resp = client.post(
        f"/agent/devices/{did}/name", json={"name": "a" * 65})
    assert resp.status_code == 400
    assert "64" in resp.get_json()["error"]


def test_rename_missing_device_returns_404(client):
    _login(client)
    resp = client.post(
        "/agent/devices/no-such-id/name", json={"name": "anything"})
    assert resp.status_code == 404


def test_rename_revoked_device_returns_410(client):
    did = _redeem(client)
    client.post(f"/agent/devices/{did}/revoke")
    resp = client.post(
        f"/agent/devices/{did}/name", json={"name": "new"})
    assert resp.status_code == 410


def test_rename_trims_whitespace(client):
    did = _redeem(client, name="x")
    resp = client.post(
        f"/agent/devices/{did}/name", json={"name": "  Padded  "})
    assert resp.status_code == 200
    assert resp.get_json()["device"]["name"] == "Padded"


# ---------------------------------------------------------------------------
# GET /settings/devices
# ---------------------------------------------------------------------------
def test_devices_page_requires_auth(client):
    other = client.application.test_client()
    resp = other.get("/settings/devices")
    assert resp.status_code in (302, 401)


def test_devices_page_empty(client):
    _login(client)
    resp = client.get("/settings/devices")
    assert resp.status_code == 200
    # No paired devices yet — the empty-state copy should render.
    assert b"No devices paired yet" in resp.data


def test_devices_page_lists_paired(client):
    did = _redeem(client, name="MyAgent")
    resp = client.get("/settings/devices")
    assert resp.status_code == 200
    assert b"MyAgent" in resp.data
    # Device id appears as the data attribute on the row.
    assert did.encode() in resp.data


def test_devices_page_shows_revoked_state(client):
    did = _redeem(client)
    client.post(f"/agent/devices/{did}/revoke")
    resp = client.get("/settings/devices")
    assert resp.status_code == 200
    # Revoked badge surface.
    assert b"revoked" in resp.data
