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
