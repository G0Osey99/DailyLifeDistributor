"""Cancel-job control-plane wiring.

handle_job_plan registers a cancel Event keyed by job_id and forwards it
to run_batch. signal_cancel(job_id) sets the matching Event so the
in-flight orchestrator's cooperative cancel-gate trips on the next row.
"""
import threading

from agent import dispatch


def test_signal_cancel_unknown_job_returns_false():
    assert dispatch.signal_cancel("does-not-exist") is False


def test_handle_job_plan_registers_cancel_event(monkeypatch):
    """run_batch must receive a cancel_event keyed by the job_id; calling
    signal_cancel on that job_id sets it."""
    captured: dict = {}

    def _fake_run(*, envelope, paths, emit, cancel_event=None):
        captured["evt"] = cancel_event
        captured["envelope"] = envelope
        emit({"type": "event", "event": "done", "job_id": envelope["job_id"]})

    monkeypatch.setattr(dispatch, "_run_batch_run", _fake_run)
    monkeypatch.setattr(dispatch, "_resolve_paths", lambda rows: {})

    sent = []

    class T:
        def send(self, frame):
            sent.append(frame)

    plan = {
        "job_id": "Jcancel",
        "rows": [{"row_idx": 0, "iso_date": "2026-05-22",
                  "platforms": ["Rock"],
                  "entry": {"date": "2026-05-22", "display_date": "May 22"},
                  "elements": {}}],
        "credentials": {},
        "config": {},
    }
    dispatch.handle_job_plan(plan=plan, transport=T())

    evt = captured.get("evt")
    assert isinstance(evt, threading.Event)
    # By the time handle_job_plan returns, the cancel Event has been
    # unregistered — signal_cancel is now a no-op for this id.
    assert dispatch.signal_cancel("Jcancel") is False


def test_signal_cancel_during_run_sets_event(monkeypatch):
    """While run_batch is in progress, signal_cancel must flip the Event
    so the orchestrator can observe it."""
    ready = threading.Event()
    observed = {}

    def _fake_run(*, envelope, paths, emit, cancel_event=None):
        # Tell the test we're alive + holding the cancel_event reference.
        observed["evt"] = cancel_event
        ready.set()
        # Wait for the test to call signal_cancel and verify.
        assert cancel_event is not None
        cancel_event.wait(timeout=2.0)
        observed["was_set"] = cancel_event.is_set()
        emit({"type": "event", "event": "done",
              "job_id": envelope["job_id"]})

    monkeypatch.setattr(dispatch, "_run_batch_run", _fake_run)
    monkeypatch.setattr(dispatch, "_resolve_paths", lambda rows: {})

    class T:
        def send(self, frame):
            pass

    plan = {"job_id": "Jrun", "rows": [],
            "credentials": {}, "config": {}}

    thread = threading.Thread(
        target=dispatch.handle_job_plan,
        kwargs=dict(plan=plan, transport=T()),
    )
    thread.start()
    assert ready.wait(2.0), "fake run never started"
    # Job is registered now — cancel should succeed.
    assert dispatch.signal_cancel("Jrun") is True
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert observed["was_set"] is True
