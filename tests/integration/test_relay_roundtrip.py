"""Browser <-> VPS relay <-> agent control round-trip over real WebSockets."""
import json
import threading
import time

import pytest

simple_websocket = pytest.importorskip("simple_websocket")


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    import importlib
    import core.db as db
    import core.devices as devices
    importlib.reload(db); importlib.reload(devices); db.init_db()
    from core import auth
    auth.reset_lockouts(); auth.set_password("pw")
    import app as flask_app_module
    importlib.reload(flask_app_module)
    app = flask_app_module.app
    app.config["TESTING"] = True

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    port = srv.socket.getsockname()[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)
    yield {"app": app, "port": port, "devices": devices}
    srv.shutdown()


def test_ping_pong_through_relay(live_server):
    port = live_server["port"]
    devices = live_server["devices"]

    code = devices.create_pairing_code()
    device_id, token = devices.redeem_pairing_code(code, "Mac")

    import requests
    s = requests.Session()
    s.post(f"http://127.0.0.1:{port}/login", data={"password": "pw"})
    cookie = "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())

    # Connect agent first, then give the server thread time to call
    # register_agent before the browser connects (so the initial presence
    # push to the browser already sees online=True).
    agent = simple_websocket.Client(
        f"ws://127.0.0.1:{port}/agent/socket?token={token}")
    time.sleep(0.3)  # let the server thread run register_agent

    browser = simple_websocket.Client(
        f"ws://127.0.0.1:{port}/agent/ws",
        headers={"Cookie": cookie})

    # Receive messages until we see online=True (handles the rare case where
    # the browser got a online=False presence before the agent registered, then
    # a second online=True broadcast after).  Give up after 3 messages.
    for _ in range(3):
        presence = json.loads(browser.receive(timeout=5))
        assert presence["type"] == "presence"
        if presence["payload"]["online"] is True:
            break
    else:
        pytest.fail(f"Never received online=True presence; last: {presence}")

    browser.send(json.dumps({"v": 1, "type": "ping", "payload": {"x": 1}}))
    got = json.loads(agent.receive(timeout=5))
    assert got["type"] == "ping" and got["payload"]["x"] == 1
    agent.send(json.dumps({"v": 1, "type": "pong", "payload": {"x": 1}}))
    pong = json.loads(browser.receive(timeout=5))
    assert pong["type"] == "pong" and pong["payload"]["x"] == 1

    agent.close(); browser.close()
