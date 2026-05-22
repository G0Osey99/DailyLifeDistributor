import json
import os

import pytest
from core import auth


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    monkeypatch.setenv("DLD_RELEASES_DIR", str(tmp_path / "releases"))
    os.makedirs(str(tmp_path / "releases"), exist_ok=True)
    import importlib
    import core.db as db, core.devices as devices, core.release_store as rs
    importlib.reload(db); importlib.reload(devices); importlib.reload(rs); db.init_db()
    auth.reset_lockouts(); auth.set_password("pw")
    import app as m; importlib.reload(m)
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        yield c, tmp_path / "releases"


def test_manifest_returns_404_when_missing(client):
    c, _ = client
    resp = c.get("/agent/releases/manifest.json")
    assert resp.status_code == 404


def test_manifest_served_when_present(client):
    c, rdir = client
    (rdir / "manifest.json").write_text(json.dumps({"version": "0.2.0"}))
    resp = c.get("/agent/releases/manifest.json")
    assert resp.status_code == 200
    assert resp.get_json() == {"version": "0.2.0"}


def test_binary_served_when_present(client):
    c, rdir = client
    (rdir / "dld-agent-windows-0.2.0.exe").write_bytes(b"BINARY_BYTES")
    resp = c.get("/agent/releases/dld-agent-windows-0.2.0.exe")
    assert resp.status_code == 200
    assert resp.data == b"BINARY_BYTES"


def test_binary_path_traversal_rejected(client):
    c, _ = client
    resp = c.get("/agent/releases/..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)


def test_release_endpoints_404_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.delenv("HYBRID_AGENT_ENABLED", raising=False)
    import importlib
    import core.db as db, core.devices as devices
    importlib.reload(db); importlib.reload(devices); db.init_db()
    auth.set_password("pw")
    import app as m; importlib.reload(m)
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        resp = c.get("/agent/releases/manifest.json")
        assert resp.status_code == 404
