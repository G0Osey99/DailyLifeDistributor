"""Cancellation: a set cancel_event short-circuits future dispatches.

In-flight dispatches finish; rows that haven't started yet emit an error
event with ``error_type: cancelled`` and skip the underlying uploader.
"""
import threading

import pytest

from agent import run_batch


def test_cancel_event_skips_dispatch_with_cancelled_error(monkeypatch):
    """When cancel_event is set BEFORE run() starts, every row emits a
    cancelled error and the real _dispatch_upload is never invoked."""
    calls = []

    def _disp(*, platform, row, emit, paths, **_):
        calls.append(platform)

    monkeypatch.setattr(run_batch, "_dispatch_upload", _disp)
    cancel = threading.Event()
    cancel.set()

    emitted = []
    run_batch.run(
        envelope={
            "rows": [
                {"row_idx": 0, "iso_date": "2026-05-22",
                 "platforms": ["YouTube Video", "Rock"],
                 "entry": {"date": "2026-05-22", "display_date": "May 22"},
                 "elements": {}},
            ],
            "config": {"max_workers": 2},
        },
        paths={"2026-05-22": {"video": "/m/v.mp4"}},
        emit=emitted.append,
        cancel_event=cancel,
    )

    assert calls == [], "no platform dispatch should run after cancel"
    cancelled = [e for e in emitted
                 if e.get("event") == "error"
                 and e.get("error_type") == "cancelled"]
    assert len(cancelled) == 2  # one per platform
    assert {c["platform"] for c in cancelled} == {"YouTube Video", "Rock"}
    # done frame still emitted for the SSE consumer.
    assert any(e.get("event") == "done" for e in emitted)


def test_cancel_event_set_mid_run_completes_in_flight(monkeypatch):
    """Tasks already past the cancel gate run to completion; future ones
    short-circuit. Verifies the cooperative-cancel semantics."""
    start_event = threading.Event()
    release_yt = threading.Event()
    completed = []

    def _disp(*, platform, row, emit, paths, **_):
        if platform == "YouTube Video":
            # Signal we're past the cancel gate, then block until released.
            start_event.set()
            release_yt.wait(2.0)
            emit({"type": "event", "event": "success",
                  "platform": platform,
                  "row_idx": row["row_idx"], "iso_date": row["iso_date"],
                  "payload": {"watch_url": "https://yt/x"}})
            completed.append(platform)
        else:
            emit({"type": "event", "event": "success",
                  "platform": platform,
                  "row_idx": row["row_idx"], "iso_date": row["iso_date"],
                  "payload": {}})
            completed.append(platform)

    monkeypatch.setattr(run_batch, "_dispatch_upload", _disp)
    cancel = threading.Event()
    emitted = []

    # Run on a worker pool of 1 so YouTube Video starts first and Rock
    # waits in the queue — when we set cancel after YT starts, Rock's
    # cancel-gate check must trigger.
    run_thread = threading.Thread(
        target=run_batch.run,
        kwargs=dict(
            envelope={
                "rows": [
                    {"row_idx": 0, "iso_date": "2026-05-22",
                     "platforms": ["YouTube Video", "Rock"],
                     "entry": {"date": "2026-05-22", "display_date": "May 22"},
                     "elements": {}},
                ],
                "config": {"max_workers": 1},
            },
            paths={"2026-05-22": {"video": "/m/v.mp4"}},
            emit=emitted.append,
            cancel_event=cancel,
        ),
        daemon=True,
    )
    run_thread.start()

    assert start_event.wait(2.0), "YouTube Video never reached dispatch"
    cancel.set()
    release_yt.set()
    run_thread.join(timeout=5.0)
    assert not run_thread.is_alive()

    # YouTube Video completed (already in-flight), Rock got cancelled.
    assert "YouTube Video" in completed
    assert "Rock" not in completed
    cancelled = [e for e in emitted
                 if e.get("event") == "error"
                 and e.get("error_type") == "cancelled"]
    assert {c["platform"] for c in cancelled} == {"Rock"}


def test_no_cancel_event_is_backward_compatible(monkeypatch):
    """Callers that don't pass cancel_event get the original behaviour."""
    calls = []

    def _disp(*, platform, row, emit, paths, **_):
        calls.append(platform)
        emit({"type": "event", "event": "success", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "payload": {}})

    monkeypatch.setattr(run_batch, "_dispatch_upload", _disp)
    emitted = []
    run_batch.run(
        envelope={
            "rows": [{"row_idx": 0, "iso_date": "2026-05-22",
                      "platforms": ["Rock"],
                      "entry": {"date": "2026-05-22", "display_date": "May 22"},
                      "elements": {}}],
            "config": {"max_workers": 2},
        },
        paths={"2026-05-22": {}},
        emit=emitted.append,
    )
    assert calls == ["Rock"]
