"""POST /upload/<job_id>/cancel routes cancel_job frames to the agent.

Covers:
  - happy path (agent online, job in registry) sends the cancel frame
  - 404 when the job_id is unknown
  - 409 when the target agent is offline
  - core.agent_dispatch.cancel_job picks up device_id from either
    register_job(device_id=...) OR the start()-recorded dispatch map
"""
import json
import queue

import pytest

from core import agent_dispatch


@pytest.fixture(autouse=True)
def _clear_jobs():
    # agent_dispatch keeps process-global registries; tests can leak state
    # otherwise.
    with agent_dispatch._jobs_lock:
        agent_dispatch._jobs.clear()
        agent_dispatch._job_dispatch_map.clear()
    yield
    with agent_dispatch._jobs_lock:
        agent_dispatch._jobs.clear()
        agent_dispatch._job_dispatch_map.clear()


# ---------------------------------------------------------------------------
# Unit tests against cancel_job directly
# ---------------------------------------------------------------------------
def test_cancel_job_uses_device_id_from_register_job(monkeypatch):
    sent = []
    monkeypatch.setattr(
        agent_dispatch._relay, "send_to_device",
        lambda did, frame: sent.append((did, frame)),
    )
    agent_dispatch.register_job(
        job_id="J1", sse_queue=queue.Queue(),
        session_id="S", device_id="device-A",
    )
    agent_dispatch.cancel_job("J1")
    assert sent == [("device-A", {"v": 1, "type": "cancel_job", "job_id": "J1"})]


def test_cancel_job_falls_back_to_dispatch_map(monkeypatch):
    """If register_job didn't carry device_id but start() recorded one
    in the dispatch map, cancel_job still works."""
    sent = []
    monkeypatch.setattr(
        agent_dispatch._relay, "send_to_device",
        lambda did, frame: sent.append((did, frame)),
    )
    with agent_dispatch._jobs_lock:
        agent_dispatch._job_dispatch_map["J2"] = "device-B"
    agent_dispatch.register_job(job_id="J2", sse_queue=queue.Queue())
    agent_dispatch.cancel_job("J2")
    assert sent[0][0] == "device-B"


def test_cancel_job_unknown_id_raises_job_not_found():
    with pytest.raises(agent_dispatch.JobNotFoundError):
        agent_dispatch.cancel_job("nope")


def test_cancel_job_offline_device_raises_agent_offline(monkeypatch):
    def _raise(did, frame):
        raise ValueError(f"device {did!r} not connected")

    monkeypatch.setattr(agent_dispatch._relay, "send_to_device", _raise)
    agent_dispatch.register_job(
        job_id="J3", sse_queue=queue.Queue(), device_id="ghost",
    )
    with pytest.raises(agent_dispatch.AgentOfflineError):
        agent_dispatch.cancel_job("J3")


def test_drop_job_clears_dispatch_map():
    with agent_dispatch._jobs_lock:
        agent_dispatch._job_dispatch_map["J4"] = "device-x"
    agent_dispatch.register_job(job_id="J4", sse_queue=queue.Queue())
    agent_dispatch.drop_job("J4")
    with agent_dispatch._jobs_lock:
        assert "J4" not in agent_dispatch._job_dispatch_map
        assert "J4" not in agent_dispatch._jobs


# ---------------------------------------------------------------------------
# Route tests (POST /upload/<job_id>/cancel)
# ---------------------------------------------------------------------------
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
    from core import auth
    auth.reset_lockouts()
    auth.set_password("correct-horse")
    import app as flask_app_module
    importlib.reload(flask_app_module)
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def _login(c):
    c.post("/login", data={"password": "correct-horse"})


def test_cancel_route_requires_auth(client):
    other = client.application.test_client()
    resp = other.post("/upload/J1/cancel")
    assert resp.status_code in (302, 401)


def test_cancel_route_404_when_job_unknown(client):
    _login(client)
    resp = client.post("/upload/no-such-job/cancel")
    assert resp.status_code == 404


def test_cancel_route_happy_path(client, monkeypatch):
    _login(client)
    sent = []

    monkeypatch.setattr(
        agent_dispatch._relay, "send_to_device",
        lambda did, frame: sent.append((did, frame)),
    )
    agent_dispatch.register_job(
        job_id="J9", sse_queue=queue.Queue(),
        session_id="S", device_id="device-Z",
    )

    resp = client.post("/upload/J9/cancel")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert sent[0][0] == "device-Z"
    assert sent[0][1]["type"] == "cancel_job"


def test_cancel_route_409_when_agent_offline(client, monkeypatch):
    _login(client)

    def _raise(did, frame):
        raise ValueError("device not connected")

    monkeypatch.setattr(agent_dispatch._relay, "send_to_device", _raise)
    agent_dispatch.register_job(
        job_id="J10", sse_queue=queue.Queue(), device_id="ghost",
    )
    resp = client.post("/upload/J10/cancel")
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["error"] == "agent offline"
