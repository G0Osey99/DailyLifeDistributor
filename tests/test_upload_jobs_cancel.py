"""Cooperative cancel for the web (in-process run_batch) upload path.

These tests cover ``core.upload_jobs``:

* ``register_job`` creates a fresh cancel ``threading.Event``.
* ``signal_cancel`` sets it; ``get_cancel_event`` returns it.
* ``run_batch`` skips remaining row dispatches once the event is set,
  emitting an ``error`` frame with ``error_type: cancelled`` instead.

A row already in flight is not interrupted — cancellation is best-effort
cooperative. The pre-submit + pre-_upload_one checks bound the worst case
to "everything still queued at signal time gets cancelled cleanly".
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from core import upload_jobs


def test_register_job_creates_cancel_event():
    upload_jobs.register_job("job-A")
    evt = upload_jobs.get_cancel_event("job-A")
    assert evt is not None
    assert isinstance(evt, threading.Event)
    assert not evt.is_set()


def test_signal_cancel_sets_event_and_returns_true():
    upload_jobs.register_job("job-B")
    assert upload_jobs.signal_cancel("job-B") is True
    evt = upload_jobs.get_cancel_event("job-B")
    assert evt is not None and evt.is_set()


def test_signal_cancel_unknown_job_returns_false():
    """No event => nothing to set => return False so the route can fall
    through to the agent dispatch (or 404)."""
    assert upload_jobs.signal_cancel("ghost") is False


def test_drop_job_removes_cancel_event():
    upload_jobs.register_job("job-C")
    upload_jobs.drop_job("job-C")
    assert upload_jobs.get_cancel_event("job-C") is None
    # Re-signal after drop is a no-op (no event in registry).
    assert upload_jobs.signal_cancel("job-C") is False


# ---------------------------------------------------------------------------
# run_batch behaviour with cancel_event
# ---------------------------------------------------------------------------

def _make_entry():
    """Stub ReviewEntry with the minimum surface run_batch touches."""
    return SimpleNamespace(
        elements=SimpleNamespace(to_dict=lambda: {}),
    )


def test_run_batch_emits_cancelled_for_remaining_rows(monkeypatch):
    """If cancel_event is already set before run_batch is invoked, every row
    short-circuits with error_type='cancelled' and no platform code runs."""
    events: list[dict] = []

    def _emit(payload):
        events.append(payload)

    dispatch_called = {"n": 0}

    def _fake_dispatch(*a, **kw):
        dispatch_called["n"] += 1
        return {"success": True, "url": "ok"}

    monkeypatch.setattr(upload_jobs, "_dispatch_upload", _fake_dispatch)

    cancel_event = threading.Event()
    cancel_event.set()  # cancel BEFORE we start

    entries = {"2026-05-22": _make_entry()}
    summary = [
        {"date": "2026-05-22", "platform": "YouTube Video",
         "iso_date": "2026-05-22", "title": "t1"},
        {"date": "2026-05-22", "platform": "SimpleCast",
         "iso_date": "2026-05-22", "title": "t2"},
    ]

    upload_jobs.run_batch(
        dates=["2026-05-22"], summary=summary,
        file_paths={}, session_id="",
        emit=_emit, entries_snapshot=entries,
        cancel_event=cancel_event,
    )

    # Neither platform actually ran.
    assert dispatch_called["n"] == 0
    # Every row got a cancelled error frame.
    cancelled = [e for e in events if e.get("error_type") == "cancelled"]
    assert len(cancelled) == 2
    assert {e["platform"] for e in cancelled} == {"YouTube Video", "SimpleCast"}


def test_run_batch_cancel_event_set_midrun_short_circuits_pending(monkeypatch):
    """Cancel set after the first row starts: that row finishes normally,
    subsequent rows emit cancelled errors instead of dispatching."""
    events: list[dict] = []
    cancel_event = threading.Event()

    def _emit(payload):
        events.append(payload)

    dispatched: list[str] = []

    def _fake_dispatch(platform, *a, **kw):
        dispatched.append(platform)
        # Trip the cancel as soon as the FIRST row's dispatcher runs.
        if len(dispatched) == 1:
            cancel_event.set()
        return {"success": True, "url": "ok"}

    monkeypatch.setattr(upload_jobs, "_dispatch_upload", _fake_dispatch)

    entries = {"2026-05-22": _make_entry()}
    summary = [
        {"date": "2026-05-22", "platform": "YouTube Video",
         "iso_date": "2026-05-22", "title": "first"},
        {"date": "2026-05-22", "platform": "SimpleCast",
         "iso_date": "2026-05-22", "title": "second"},
        {"date": "2026-05-22", "platform": "Vista Social",
         "iso_date": "2026-05-22", "title": "third"},
    ]

    # max_workers=1 so we serialise (one in-flight at a time) and the
    # cancel signal lands BETWEEN row dispatches, not in parallel with one.
    upload_jobs.run_batch(
        dates=["2026-05-22"], summary=summary,
        file_paths={}, session_id="",
        emit=_emit, entries_snapshot=entries,
        cancel_event=cancel_event,
        config={"upload": {"max_workers": 1}},
    )

    # First row dispatched; later rows didn't.
    assert len(dispatched) == 1
    cancelled = [e for e in events if e.get("error_type") == "cancelled"]
    # Both remaining rows emit one cancelled frame each.
    plats = {e["platform"] for e in cancelled}
    assert "SimpleCast" in plats
    assert "Vista Social" in plats


def test_run_batch_no_cancel_event_is_backward_compatible(monkeypatch):
    """Omitting cancel_event must preserve today's behaviour. No row
    short-circuits; everything dispatches as usual."""
    events: list[dict] = []
    dispatched: list[str] = []

    def _fake_dispatch(platform, *a, **kw):
        dispatched.append(platform)
        return {"success": True, "url": "ok"}

    monkeypatch.setattr(upload_jobs, "_dispatch_upload", _fake_dispatch)

    entries = {"2026-05-22": _make_entry()}
    summary = [
        {"date": "2026-05-22", "platform": "YouTube Video",
         "iso_date": "2026-05-22", "title": "t1"},
        {"date": "2026-05-22", "platform": "SimpleCast",
         "iso_date": "2026-05-22", "title": "t2"},
    ]

    upload_jobs.run_batch(
        dates=["2026-05-22"], summary=summary,
        file_paths={}, session_id="",
        emit=lambda p: events.append(p), entries_snapshot=entries,
        # cancel_event omitted on purpose
    )
    assert sorted(dispatched) == ["SimpleCast", "YouTube Video"]
    assert not any(e.get("error_type") == "cancelled" for e in events)
