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


def test_pair_new_requires_auth(client):
    resp = client.post("/agent/pair/new")
    assert resp.status_code in (302, 401)  # redirect to login or JSON 401


def test_pair_redeem_roundtrip(client):
    _login(client)
    code = client.post("/agent/pair/new").get_json()["code"]
    # Redeem is public (the agent has no session yet).
    client2 = client.application.test_client()
    resp = client2.post("/agent/pair/redeem", json={"code": code, "name": "Mac"})
    assert resp.status_code == 200
    assert resp.get_json()["device_id"]
    assert resp.get_json()["token"]


def test_list_and_revoke(client):
    _login(client)
    code = client.post("/agent/pair/new").get_json()["code"]
    client.application.test_client().post(
        "/agent/pair/redeem", json={"code": code, "name": "Mac"})
    devs = client.get("/agent/devices").get_json()["devices"]
    assert len(devs) == 1
    did = devs[0]["id"]
    assert client.post(f"/agent/devices/{did}/revoke").status_code == 200
    devs = client.get("/agent/devices").get_json()["devices"]
    assert devs[0]["revoked"] == 1
