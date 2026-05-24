# agent/tests/test_dispatch.py
"""Tests for agent/dispatch.py — B7."""
from agent import dispatch


class StubTransport:
    def __init__(self):
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)


# ---------------------------------------------------------------------------
# Minimal plan fixture
# ---------------------------------------------------------------------------
_PLAN = {
    "v": 1,
    "type": "job_plan",
    "job_id": "J1",
    "protocol_version": 1,
    "config": {"max_workers": 4},
    "rows": [
        {
            "row_idx": 0,
            "iso_date": "2026-05-22",
            "platforms": ["YouTube Video"],
            "entry": {"date": "2026-05-22", "display_date": "May 22, 2026"},
            "elements": {},
        }
    ],
    "credentials": {"youtube.token": "{}"},
}


def test_handle_job_plan_installs_creds_and_runs_then_emits_done(monkeypatch):
    """handle_job_plan resolves paths, passes envelope to run_batch, stamps
    job_id on every frame, and sends them through the transport."""
    # Stub path resolver so no filesystem required.
    monkeypatch.setattr(
        dispatch, "_resolve_paths",
        lambda rows: {"2026-05-22": {"video": "/m/v.mp4"}},
    )

    # Stub run_batch to emit a couple of canonical frames then done.
    seen = {}

    def _fake_run(*, envelope, paths, emit, cancel_event=None):
        seen["envelope_job"] = envelope["job_id"]
        seen["paths"] = paths
        emit({"type": "event", "event": "start", "platform": "YouTube Video",
              "row_idx": 0, "iso_date": "2026-05-22"})
        emit({"type": "event", "event": "done"})

    monkeypatch.setattr(dispatch, "_run_batch_run", _fake_run)

    transport = StubTransport()
    dispatch.handle_job_plan(plan=_PLAN, transport=transport)

    # run_batch received the correct job_id and resolved paths.
    assert seen["envelope_job"] == "J1"
    assert seen["paths"]["2026-05-22"]["video"] == "/m/v.mp4"

    # Every frame was sent through the transport and carries job_id.
    types = [(f["type"], f.get("event")) for f in transport.sent]
    assert ("event", "start") in types
    assert ("event", "done") in types
    assert all(f.get("job_id") == "J1" for f in transport.sent), (
        "some frames missing job_id"
    )


def test_handle_job_plan_stamps_job_id_on_frames_that_already_have_it(monkeypatch):
    """Frames that already carry job_id must not be double-stamped."""
    monkeypatch.setattr(dispatch, "_resolve_paths", lambda rows: {})

    def _fake_run(*, envelope, paths, emit, cancel_event=None):
        emit({"type": "event", "event": "done", "job_id": "J1"})

    monkeypatch.setattr(dispatch, "_run_batch_run", _fake_run)

    transport = StubTransport()
    dispatch.handle_job_plan(plan=_PLAN, transport=transport)

    done_frames = [f for f in transport.sent if f.get("event") == "done"]
    assert len(done_frames) == 1
    assert done_frames[0]["job_id"] == "J1"


def test_handle_job_plan_emits_error_and_done_on_run_batch_crash(monkeypatch):
    """If run_batch raises, dispatch catches it, emits error + done."""
    monkeypatch.setattr(dispatch, "_resolve_paths", lambda rows: {})

    def _crash(*, envelope, paths, emit, cancel_event=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(dispatch, "_run_batch_run", _crash)

    transport = StubTransport()
    dispatch.handle_job_plan(plan=_PLAN, transport=transport)

    events = [f.get("event") for f in transport.sent]
    assert "error" in events
    assert "done" in events


def test_handle_job_plan_calls_shim_shutdown_on_success(monkeypatch):
    """The Shim returned by install_as_core_secrets_store must be shut down
    in handle_job_plan's finally block so credentials don't linger."""
    monkeypatch.setattr(dispatch, "_resolve_paths", lambda rows: {})
    monkeypatch.setattr(dispatch, "_run_batch_run",
                        lambda *, envelope, paths, emit, cancel_event=None: None)

    shims_returned = []
    real_install = dispatch._sshim.install_as_core_secrets_store

    def _spy_install(*, initial, emit):
        shim = real_install(initial=initial, emit=emit)
        shims_returned.append(shim)
        return shim

    monkeypatch.setattr(dispatch._sshim, "install_as_core_secrets_store",
                        _spy_install)

    dispatch.handle_job_plan(plan=_PLAN, transport=StubTransport())

    assert len(shims_returned) == 1
    assert shims_returned[0]._closed is True, (
        "shim.shutdown() must be called in handle_job_plan finally"
    )


def test_handle_job_plan_calls_shim_shutdown_even_on_crash(monkeypatch):
    """Crash path must also call shim.shutdown() (finally semantics)."""
    monkeypatch.setattr(dispatch, "_resolve_paths", lambda rows: {})

    def _crash(*, envelope, paths, emit, cancel_event=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(dispatch, "_run_batch_run", _crash)

    shims_returned = []
    real_install = dispatch._sshim.install_as_core_secrets_store

    def _spy_install(*, initial, emit):
        shim = real_install(initial=initial, emit=emit)
        shims_returned.append(shim)
        return shim

    monkeypatch.setattr(dispatch._sshim, "install_as_core_secrets_store",
                        _spy_install)

    dispatch.handle_job_plan(plan=_PLAN, transport=StubTransport())

    assert shims_returned[0]._closed is True


def test_resolve_paths_uses_latest_results_when_cached(monkeypatch):
    """_resolve_paths returns paths from the scan cache without re-scanning."""
    # Prime the module-level cache directly.
    import agent.scan as _s
    with _s._last_lock:
        _s._last_results.clear()
        _s._last_results.update({"2026-05-22": {"video": "/cached/v.mp4"}})

    rows = [{"iso_date": "2026-05-22"}]
    result = dispatch._resolve_paths(rows)
    assert result["2026-05-22"]["video"] == "/cached/v.mp4"

    # Clean up cache.
    with _s._last_lock:
        _s._last_results.clear()


def test_resolve_paths_falls_back_to_fresh_scan_when_cache_empty(monkeypatch, tmp_path):
    """_resolve_paths calls scan.scan() if latest_results() is empty."""
    from agent import scan as _s

    # Ensure cache is empty.
    with _s._last_lock:
        _s._last_results.clear()

    # Provide a temporary video directory with a date-named file.
    vid_dir = tmp_path / "video"
    vid_dir.mkdir()
    (vid_dir / "260522_episode.mp4").write_bytes(b"x")

    _s.set_roots({"video": str(vid_dir)})
    try:
        rows = [{"iso_date": "2026-05-22"}]
        result = dispatch._resolve_paths(rows)
        assert "video" in result.get("2026-05-22", {}), (
            "fresh scan should have found the video file"
        )
    finally:
        _s.set_roots({})
        with _s._last_lock:
            _s._last_results.clear()
