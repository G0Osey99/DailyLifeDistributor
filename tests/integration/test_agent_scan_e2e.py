"""End-to-end: browser asks the agent (over the relay) to scan local media."""
import json
import threading
import time

import pytest

simple_websocket = pytest.importorskip("simple_websocket")


def _touch(p):
    with open(p, "w") as f:
        f.write("x")


@pytest.fixture()
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    # Multi-tenant phase α: legacy shared-password form (opt-in).
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "true")
    import importlib
    import core.db as db, core.devices as devices
    importlib.reload(db); importlib.reload(devices); db.init_db()
    from core import auth
    auth.reset_lockouts(); auth.set_password("pw")
    import app as m; importlib.reload(m)
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, m.app, threaded=True)
    port = srv.socket.getsockname()[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield port, devices, tmp_path
    srv.shutdown()


def test_scan_request_roundtrip(live, monkeypatch):
    port, devices, tmp_path = live
    server_url = f"http://127.0.0.1:{port}"

    vids = tmp_path / "vids"; vids.mkdir()
    _touch(vids / "260115_sermon.mp4")
    _touch(vids / "260116_sermon.mp4")

    code = devices.create_pairing_code()
    _, token = devices.redeem_pairing_code(code, "Mac")
    from agent import config
    monkeypatch.setattr(config, "get_token", lambda: token)
    monkeypatch.setattr(config, "get_server_url", lambda: server_url)
    monkeypatch.setattr(config, "get_media_roots", lambda: {"video": str(vids)})

    from agent import main as agent_main
    threading.Thread(target=agent_main.run, args=(server_url,), daemon=True).start()
    time.sleep(0.8)

    import requests
    s = requests.Session(); s.post(f"{server_url}/login", data={"password": "pw"})
    cookie = "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())
    browser = simple_websocket.Client(f"ws://127.0.0.1:{port}/agent/ws",
                                      headers={"Cookie": cookie})
    for _ in range(5):
        first = json.loads(browser.receive(timeout=5))
        if first.get("type") == "presence" and first["payload"]["online"] is True:
            break
    else:
        pytest.fail("agent never reported presence=online within 5 messages")

    browser.send(json.dumps({"v": 1, "type": "scan_request", "payload": {}}))
    for _ in range(10):
        result = json.loads(browser.receive(timeout=5))
        if result.get("type") == "scan_result":
            break
    else:
        pytest.fail("scan_result never received within 10 messages")
    assert result["type"] == "scan_result"
    assert result["payload"]["dates"] == ["2026-01-15", "2026-01-16"]
    assert result["payload"]["by_date"]["2026-01-15"]["video"] == ["260115_sermon.mp4"]
    browser.close()
