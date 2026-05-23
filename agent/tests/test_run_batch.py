# agent/tests/test_run_batch.py
import pytest
from agent import run_batch


@pytest.fixture
def stub_dispatch(monkeypatch):
    calls = []

    def _dispatch(*, platform, row, emit, paths, **_):
        calls.append({"platform": platform, "row_idx": row["row_idx"]})
        emit({"type": "event", "event": "success", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "payload": {}})
        return {"success": True}

    monkeypatch.setattr(run_batch, "_dispatch_upload", _dispatch)
    return calls


def test_run_batch_dispatches_each_row_platform_combination(stub_dispatch):
    emitted = []
    envelope = {
        "rows": [
            {"row_idx": 0, "iso_date": "2026-05-22",
             "platforms": ["YouTube Video", "Rock"],
             "entry": {"date": "2026-05-22", "display_date": "May 22, 2026"}, "elements": {}},
            {"row_idx": 1, "iso_date": "2026-05-23",
             "platforms": ["Simplecast"],
             "entry": {"date": "2026-05-23", "display_date": "May 23, 2026"}, "elements": {}},
        ],
        "config": {"max_workers": 4},
    }
    paths = {
        "2026-05-22": {"video": "/m/v22.mp4"},
        "2026-05-23": {"audio": "/m/a23.mp3"},
    }
    run_batch.run(envelope=envelope, paths=paths, emit=emitted.append)
    assert sorted((c["row_idx"], c["platform"]) for c in stub_dispatch) == [
        (0, "Rock"), (0, "YouTube Video"), (1, "Simplecast"),
    ]
    assert any(e.get("event") == "done" for e in emitted)


# ---------------------------------------------------------------------------
# B5: circuit breaker + email-after-YouTube ordering
# ---------------------------------------------------------------------------

def test_rock_email_waits_for_youtube_video_result(monkeypatch):
    """Email dispatcher must see the watch_url from the YouTube row."""
    import threading, time
    seen = {}
    yt_done_evt = threading.Event()

    def _disp(*, platform, row, emit, paths, **_):
        if platform == "YouTube Video":
            time.sleep(0.05)
            emit({"type": "event", "event": "success", "platform": "YouTube Video",
                  "row_idx": row["row_idx"], "iso_date": row["iso_date"],
                  "payload": {"watch_url": "https://yt/x"}})
            yt_done_evt.set()
            return {"success": True}
        if platform == "Rock Email":
            assert yt_done_evt.wait(2.0), "email started before YT finished"
            seen["watch_url_at_email_start"] = row.get("yt_watch_url")
            emit({"type": "event", "event": "success", "platform": "Rock Email",
                  "row_idx": row["row_idx"], "iso_date": row["iso_date"], "payload": {}})
            return {"success": True}

    monkeypatch.setattr(run_batch, "_dispatch_upload", _disp)
    emitted = []
    run_batch.run(
        envelope={
            "rows": [{"row_idx": 0, "iso_date": "2026-05-22",
                      "platforms": ["YouTube Video", "Rock Email"],
                      "entry": {"date": "2026-05-22", "display_date": "May 22, 2026"},
                      "elements": {}}],
            "config": {"max_workers": 4},
        },
        paths={"2026-05-22": {"video": "/m/v.mp4"}},
        emit=emitted.append,
    )
    assert seen["watch_url_at_email_start"] == "https://yt/x"


def test_circuit_breaker_short_circuits_after_threshold(monkeypatch):
    """3 consecutive transient failures trip the breaker; 4th+ call is skipped."""
    from core import circuit_breaker
    circuit_breaker.reset_all()  # isolate from other tests

    calls = {"n": 0}

    def _disp(*, platform, row, emit, paths, **_):
        calls["n"] += 1
        raise TimeoutError("network")

    monkeypatch.setattr(run_batch, "_dispatch_upload", _disp)
    emitted = []
    rows = [
        {"row_idx": i, "iso_date": f"2026-05-{20 + i:02d}",
         "platforms": ["Rock"],
         "entry": {"date": f"2026-05-{20 + i:02d}", "display_date": f"May {20 + i}, 2026"},
         "elements": {}}
        for i in range(5)
    ]
    run_batch.run(
        envelope={
            "rows": rows,
            "config": {
                "max_workers": 1,
                "circuit_breaker": {"failure_threshold": 3,
                                    "recovery_timeout_seconds": 60},
            },
        },
        paths={r["iso_date"]: {} for r in rows},
        emit=emitted.append,
    )
    assert calls["n"] == 3
    circuit_breaker.reset_all()  # clean up


# ---------------------------------------------------------------------------
# B6: real per-platform dispatch — YouTube path resolution
# ---------------------------------------------------------------------------

def test_dispatch_calls_youtube_uploader_with_resolved_video_path(monkeypatch, tmp_path):
    from uploaders import youtube_uploader
    called = {}

    def _fake_upload(entry, is_short=False, dry_run=False, elements=None,
                     progress_callback=None, event_callback=None):
        called["video_path"] = entry.youtube_video_path
        called["is_short"] = is_short
        return {"success": True, "watch_url": "https://yt/y",
                "video_id": "abc", "url": "https://yt/y", "scheduled_time": None}

    monkeypatch.setattr(youtube_uploader, "upload_video", _fake_upload)
    emitted = []
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    run_batch.run(
        envelope={
            "rows": [{"row_idx": 0, "iso_date": "2026-05-22",
                      "platforms": ["YouTube Video"],
                      "entry": {"date": "2026-05-22", "display_date": "May 22, 2026",
                                "youtube_title": "T"},
                      "elements": {"yt_video_enabled": True,
                                   "yt_video_thumbnail": False,
                                   "yt_video_schedule": False}}],
            "config": {"max_workers": 1},
        },
        paths={"2026-05-22": {"video": str(video)}},
        emit=emitted.append,
    )
    assert called["video_path"] == str(video)
    assert called["is_short"] is False


# ---------------------------------------------------------------------------
# Phase 3: per-run state, breaker reset, Rock-Email aborts on missing YT URL
# ---------------------------------------------------------------------------


def test_sequential_runs_do_not_leak_yt_state(monkeypatch):
    """Two consecutive run() calls must not share YT done/url state.

    Regression: previously _yt_done / _yt_url were module-level dicts; a
    second run with the same row_idx would short-circuit _wait_yt against
    the first run's result.
    """
    seen_watch_urls = []

    def _disp(*, platform, row, emit, paths, **_):
        if platform == "YouTube Video":
            emit({"type": "event", "event": "success", "platform": "YouTube Video",
                  "row_idx": row["row_idx"], "iso_date": row["iso_date"],
                  "payload": {"watch_url": f"https://yt/{row['iso_date']}"}})
            return {"success": True}
        if platform == "Rock Email":
            seen_watch_urls.append(row.get("yt_watch_url"))
            emit({"type": "event", "event": "success", "platform": "Rock Email",
                  "row_idx": row["row_idx"], "iso_date": row["iso_date"],
                  "payload": {}})
            return {"success": True}

    monkeypatch.setattr(run_batch, "_dispatch_upload", _disp)

    # Run A
    run_batch.run(
        envelope={
            "rows": [{"row_idx": 0, "iso_date": "2026-05-22",
                      "platforms": ["YouTube Video", "Rock Email"],
                      "entry": {"date": "2026-05-22", "display_date": "May 22, 2026"},
                      "elements": {}}],
            "config": {"max_workers": 4},
        },
        paths={"2026-05-22": {}}, emit=lambda f: None,
    )

    # Run B — same row_idx, different date. With the old module-level
    # state, the email row would immediately resolve to run A's URL.
    run_batch.run(
        envelope={
            "rows": [{"row_idx": 0, "iso_date": "2026-05-23",
                      "platforms": ["YouTube Video", "Rock Email"],
                      "entry": {"date": "2026-05-23", "display_date": "May 23, 2026"},
                      "elements": {}}],
            "config": {"max_workers": 4},
        },
        paths={"2026-05-23": {}}, emit=lambda f: None,
    )

    assert seen_watch_urls == ["https://yt/2026-05-22", "https://yt/2026-05-23"], (
        f"YT state leaked between runs: {seen_watch_urls}"
    )


def test_run_resets_circuit_breakers_so_a_tripped_breaker_does_not_block(monkeypatch):
    """If a breaker was tripped to OPEN in a previous run, run() must
    reset it so the new run can dispatch."""
    from core import circuit_breaker

    # Force-trip the breaker that _run_one will pick up for "Rock".
    br = circuit_breaker.get_breaker(
        "upload:Rock", failure_threshold=1, recovery_timeout=999999.0,
    )
    br.record_failure()  # threshold=1 ⇒ OPEN
    assert not br.allow(), "precondition: breaker should be OPEN"

    calls = []

    def _disp(*, platform, row, emit, paths, **_):
        calls.append(platform)
        emit({"type": "event", "event": "success", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"], "payload": {}})
        return {"success": True}

    monkeypatch.setattr(run_batch, "_dispatch_upload", _disp)

    run_batch.run(
        envelope={
            "rows": [{"row_idx": 0, "iso_date": "2026-05-22",
                      "platforms": ["Rock"],
                      "entry": {"date": "2026-05-22", "display_date": "May 22, 2026"},
                      "elements": {}}],
            "config": {"max_workers": 1},
        },
        paths={"2026-05-22": {}}, emit=lambda f: None,
    )
    assert calls == ["Rock"], (
        f"breaker was not reset; dispatch was blocked. calls={calls}"
    )


def test_rock_email_aborts_when_yt_failed_no_url(monkeypatch):
    """If YT Video fails (no success event), the Rock Email row for the
    same date must NOT call rock_schedule_email — it should error out
    immediately and surface a meaningful message."""
    from uploaders.rock import email as rock_email
    from uploaders import youtube_uploader

    # YT upload fails — emit no success event, return error result.
    def _fake_yt(entry, is_short=False, dry_run=False, elements=None,
                 progress_callback=None, event_callback=None):
        return {"success": False, "error": "boom"}

    schedule_calls = []

    def _fake_schedule(entry, *, youtube_watch_url, elements=None,
                       progress_callback=None):
        schedule_calls.append(youtube_watch_url)
        return {"success": True}

    monkeypatch.setattr(youtube_uploader, "upload_video", _fake_yt)
    monkeypatch.setattr(rock_email, "schedule_email", _fake_schedule)

    emitted = []
    run_batch.run(
        envelope={
            "rows": [{"row_idx": 0, "iso_date": "2026-05-22",
                      "platforms": ["YouTube Video", "Rock Email"],
                      "entry": {"date": "2026-05-22", "display_date": "May 22, 2026"},
                      "elements": {}}],
            "config": {"max_workers": 4},
        },
        paths={"2026-05-22": {}}, emit=emitted.append,
    )

    # rock_schedule_email must NOT have been called.
    assert schedule_calls == [], (
        f"rock_schedule_email should not run when YT failed; got {schedule_calls}"
    )
    # An error event for Rock Email must have been emitted.
    email_errors = [f for f in emitted
                    if f.get("type") == "event"
                    and f.get("event") == "error"
                    and f.get("platform") == "Rock Email"]
    assert email_errors, f"no Rock Email error event emitted: {emitted}"
