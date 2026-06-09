"""POST /upload/<job_id>/cancel covers both the web and agent upload paths.

The route first consults the upload_jobs registry (web path: an in-process
``threading.Event`` for the run_batch worker); if no entry exists it falls
back to ``agent_dispatch.cancel_job`` (agent path: a ``cancel_job`` relay
frame forwarded to the device).
"""
from __future__ import annotations

import importlib
import queue

import pytest

from core import agent_dispatch, upload_jobs


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
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


def test_cancel_web_path_sets_event_and_returns_ok(client):
    """A web-path job (registered via upload_jobs.register_job) is cancelled
    by setting its threading.Event in the in-process registry."""
    _login(client)
    upload_jobs.register_job("web-job-1")
    evt = upload_jobs.get_cancel_event("web-job-1")
    assert evt is not None and not evt.is_set()

    resp = client.post("/upload/web-job-1/cancel")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert evt.is_set()


def test_cancel_web_path_takes_precedence_over_agent(client, monkeypatch):
    """When a job is registered on BOTH paths (defence-in-depth), the
    web-path Event wins — we never even reach the agent dispatch lookup."""
    _login(client)
    upload_jobs.register_job("dual-job")

    agent_called = {"n": 0}

    def _spy_cancel(job_id):
        agent_called["n"] += 1

    monkeypatch.setattr(agent_dispatch, "cancel_job", _spy_cancel)

    resp = client.post("/upload/dual-job/cancel")
    assert resp.status_code == 200
    assert upload_jobs.get_cancel_event("dual-job").is_set()
    # The agent dispatch was NOT consulted — the web path short-circuits it.
    assert agent_called["n"] == 0


def test_cancel_agent_path_forwards_to_relay(client, monkeypatch):
    """When the job is only registered in agent_dispatch (the relay-fed
    agent path), we fall through to its cancel_job and emit a relay frame."""
    _login(client)
    sent = []
    monkeypatch.setattr(
        agent_dispatch._relay, "send_to_device",
        lambda did, frame: sent.append((did, frame)),
    )
    agent_dispatch.register_job(
        job_id="agent-job-1", sse_queue=queue.Queue(),
        session_id="S", device_id="device-X",
    )
    # Critically, NO upload_jobs.register_job call — this is agent-only.
    assert upload_jobs.get_cancel_event("agent-job-1") is None

    resp = client.post("/upload/agent-job-1/cancel")
    assert resp.status_code == 200
    assert sent and sent[0][0] == "device-X"
    assert sent[0][1]["type"] == "cancel_job"


def test_cancel_unknown_job_returns_404(client):
    """No entry in either registry → 404."""
    _login(client)
    resp = client.post("/upload/no-such-job/cancel")
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "job not found"


def test_cancel_route_requires_auth(tmp_path, monkeypatch):
    """Unauthenticated request is rejected (redirect to login)."""
    # CRITICAL: reload(db) re-evaluates _DB_PATH from the environment and
    # discards the conftest isolation patch. Without redirecting
    # DLD_STATE_DB first (as the `client` fixture above does), this test
    # reloaded the REAL repo state.db and then auth.set_password() +
    # the app reload's bootstrap/migrate steps WROTE to it — corrupting
    # the developer's actual secrets (password hash + imported API keys,
    # encrypted under this test's throwaway Fernet key).
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    import core.db as db
    import core.devices as devices
    importlib.reload(db)
    importlib.reload(devices)
    db.init_db()
    from core import auth
    auth.reset_lockouts()
    auth.set_password("p")
    import app as flask_app_module
    importlib.reload(flask_app_module)
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        resp = c.post("/upload/J1/cancel")
    assert resp.status_code in (302, 401)
