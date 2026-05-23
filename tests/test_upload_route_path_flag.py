"""Tests for /media/batch/run?path=agent flag (Task A10).

Verifies that:
  1. path=agent + HYBRID_AGENT_ENABLED=true  → agent_dispatch.start called
  2. no path flag (web default)              → upload_jobs._run_batch_worker thread launched
  3. path=agent + HYBRID_AGENT_ENABLED unset → falls through to web path
"""
from __future__ import annotations

import importlib
import os
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_client(monkeypatch, tmp_path, hybrid_enabled: bool):
    """Build a test Flask client with auth bypassed and run-validation mocked."""
    if hybrid_enabled:
        monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    else:
        monkeypatch.delenv("HYBRID_AGENT_ENABLED", raising=False)

    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))

    from core import auth, db as _db
    importlib.reload(_db)
    _db.init_db()
    auth.reset_lockouts()
    auth.set_password("test-password")

    import app as flask_app_module
    importlib.reload(flask_app_module)
    flask_app_module.app.config["TESTING"] = True
    flask_app_module.app.config["WTF_CSRF_ENABLED"] = False

    client = flask_app_module.app.test_client()

    # Log in so auth gate passes.
    client.post("/login", data={"password": "test-password"})

    # Patch _active_run in blueprints.media so batch_run skips run-lock validation.
    import blueprints.media as media_mod
    fake_run = {
        "files": {},
        "dir": None,
        "bytes_total": 0,
    }
    monkeypatch.setattr(media_mod, "_active_run", lambda run_id: fake_run)

    # Patch _release_run so it doesn't try to touch fake_run["dir"] (None);
    # tests that care about whether it was called inspect _release_calls.
    _release_calls: list = []
    def _fake_release(run_id):
        _release_calls.append(run_id)
    monkeypatch.setattr(media_mod, "_release_run", _fake_release)

    # Patch _apply_paths_to_session to no-op (no spreadsheet in test).
    monkeypatch.setattr(media_mod, "_apply_paths_to_session",
                        lambda *a, **kw: None)

    # Patch session.get_summary and session.entries to return empty.
    from core.session_state import session as _session
    monkeypatch.setattr(_session, "get_summary", lambda: [], raising=False)
    monkeypatch.setattr(_session, "entries", {}, raising=False)

    # Expose the spy + the app for tests that want to inspect release calls.
    flask_app_module.app._test_release_calls = _release_calls  # type: ignore[attr-defined]

    return client, flask_app_module.app


@pytest.fixture
def app_with_hybrid_enabled(tmp_path, monkeypatch):
    client, _app = _make_client(monkeypatch, tmp_path, hybrid_enabled=True)
    return client


@pytest.fixture
def app_without_hybrid(tmp_path, monkeypatch):
    client, _app = _make_client(monkeypatch, tmp_path, hybrid_enabled=False)
    return client


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Default client — HYBRID_AGENT_ENABLED unset (web path)."""
    c, _app = _make_client(monkeypatch, tmp_path, hybrid_enabled=False)
    return c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BATCH_BODY = {"run_id": "test-run", "dates": [], "platforms": [], "files": {}}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_upload_path_agent_dispatches_to_agent_dispatch(
        monkeypatch, tmp_path, app_with_hybrid_enabled):
    """path=agent + flag on → agent_dispatch.start called, returns job_id."""
    from core import agent_dispatch
    called: dict = {}

    def _fake_start(**kw):
        called.update(kw)
        return "JX"

    monkeypatch.setattr(agent_dispatch, "start", _fake_start)

    r = app_with_hybrid_enabled.post(
        "/media/batch/run?path=agent",
        json=_BATCH_BODY,
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["job_id"] == "JX"
    assert called, "agent_dispatch.start was not invoked"


def test_upload_path_agent_releases_run_lock_after_dispatch(
        monkeypatch, tmp_path):
    """After a successful agent dispatch, the RunLock + per-run temp dir
    must be released so a subsequent /media/run/init can succeed and the
    batch's temp files don't leak through the agent upload."""
    client, app = _make_client(monkeypatch, tmp_path, hybrid_enabled=True)
    release_calls = app._test_release_calls  # type: ignore[attr-defined]

    from core import agent_dispatch
    monkeypatch.setattr(agent_dispatch, "start", lambda **kw: "JREL")

    r = client.post("/media/batch/run?path=agent", json={**_BATCH_BODY, "run_id": "R1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert release_calls == ["R1"], (
        f"_release_run was not called for the agent path: {release_calls}"
    )


def test_upload_path_agent_releases_run_lock_on_no_agent_online(
        monkeypatch, tmp_path):
    """If agent_dispatch.start raises NoAgentOnlineError, the RunLock and
    temp dir must still be released — otherwise the next run is wedged."""
    client, app = _make_client(monkeypatch, tmp_path, hybrid_enabled=True)
    release_calls = app._test_release_calls  # type: ignore[attr-defined]

    from core import agent_dispatch

    def _raise(**kw):
        raise agent_dispatch.NoAgentOnlineError("no agent")

    monkeypatch.setattr(agent_dispatch, "start", _raise)

    r = client.post("/media/batch/run?path=agent", json={**_BATCH_BODY, "run_id": "R2"})
    assert r.status_code == 409
    assert r.get_json().get("error") == "no_agent_online"
    assert release_calls == ["R2"], (
        f"_release_run was not called on NoAgentOnlineError: {release_calls}"
    )


def test_upload_path_agent_passes_real_elements(monkeypatch, tmp_path):
    """elements dict passed to agent_dispatch.start reflects per-entry UploadElements."""
    from core.session_state import ReviewEntry, UploadElements, session as _session

    # Build a fake entry with non-default elements (sc_enabled=False to verify
    # the dict carries real field values, not just defaults).
    fake_elements = UploadElements(sc_enabled=False)
    fake_entry = ReviewEntry(date="2026-01-01", display_date="Jan 1, 2026",
                              elements=fake_elements)

    client, _app = _make_client(monkeypatch, tmp_path, hybrid_enabled=True)

    # Override the empty entries/get_summary stubs with real data for this test.
    monkeypatch.setattr(_session, "entries", {"2026-01-01": fake_entry}, raising=False)
    monkeypatch.setattr(_session, "get_summary",
                        lambda: [{"date": "2026-01-01", "iso_date": "2026-01-01",
                                  "platforms": ["Simplecast"]}],
                        raising=False)

    from core import agent_dispatch
    captured: dict = {}

    def _fake_start(**kw):
        captured.update(kw)
        return "JY"

    monkeypatch.setattr(agent_dispatch, "start", _fake_start)

    r = client.post("/media/batch/run?path=agent", json=_BATCH_BODY)
    assert r.status_code == 200, r.get_data(as_text=True)

    elements = captured.get("elements", {})
    assert elements, "elements must be non-empty when entries exist"
    entry_elements = elements.get("2026-01-01", {})
    assert entry_elements, "elements dict must be keyed by iso_date"
    # Verify the real field value propagated (not just defaults).
    assert entry_elements.get("sc_enabled") is False, (
        "UploadElements.sc_enabled=False must appear in the elements dict"
    )


def test_upload_path_web_keeps_running_run_batch(monkeypatch, client):
    """No path flag → existing web path: background thread with run_batch launched."""
    import blueprints.media as media_mod
    called = {"worker": False}

    # Intercept the thread start by patching _run_batch_worker.
    real_thread_start = None

    import threading

    original_thread_class = threading.Thread

    class _CapturingThread:
        def __init__(self, target=None, *args, **kwargs):
            self._target = target
            self._kwargs = kwargs

        def start(self):
            called["worker"] = True  # worker was scheduled

    monkeypatch.setattr(media_mod.threading, "Thread", _CapturingThread)

    r = client.post("/media/batch/run", json=_BATCH_BODY)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert called["worker"] is True, "_run_batch_worker thread was not started"


def test_upload_path_agent_with_flag_off_falls_through_to_web(
        monkeypatch, tmp_path, app_without_hybrid):
    """path=agent but HYBRID_AGENT_ENABLED unset → falls through to web path."""
    from core import agent_dispatch
    import blueprints.media as media_mod

    monkeypatch.setattr(agent_dispatch, "start",
                        lambda **kw: pytest.fail("agent_dispatch.start must not run"))

    called = {"worker": False}

    class _CapturingThread:
        def __init__(self, target=None, *args, **kwargs):
            pass

        def start(self):
            called["worker"] = True

    monkeypatch.setattr(media_mod.threading, "Thread", _CapturingThread)

    r = app_without_hybrid.post(
        "/media/batch/run?path=agent",
        json=_BATCH_BODY,
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    assert called["worker"] is True, "expected web path (_run_batch_worker) to run"
