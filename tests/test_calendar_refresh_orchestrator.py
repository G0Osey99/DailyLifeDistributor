
from core.calendar_refresh import (
    ExternalItem,
    SessionExpiredError,
    run_refresh,
)


def _ok_source(name, items, platforms=None):
    class S:
        NAME = name
        PLATFORMS = platforms or [name]
        @staticmethod
        def fetch(window_start, window_end):
            return items
    return S


def _bad_source(name, exc, platforms=None):
    class S:
        NAME = name
        PLATFORMS = platforms or [name]
        @staticmethod
        def fetch(window_start, window_end):
            raise exc
    return S


def test_item_to_dict_roundtrip():
    it = ExternalItem(
        platform="youtube_video", external_id="x", iso_date="2026-05-10",
        scheduled_time="2026-05-10T08:00:00-04:00",
        title="t", url="u", status="scheduled", raw_json="{}",
    )
    d = it.to_dict()
    assert d["platform"] == "youtube_video"
    assert d["external_id"] == "x"


def test_run_refresh_aggregates_per_source(temp_db):
    yt = _ok_source("youtube_video", [
        ExternalItem("youtube_video", "v1", "2026-05-10", "", "t", "u", "scheduled", "{}")
    ])
    rk = _ok_source("rock", [])
    out = run_refresh(sources=[yt, rk], window_days_back=30, window_days_forward=60)
    assert out["results"]["youtube_video"] == {"ok": True, "count": 1}
    assert out["results"]["rock"] == {"ok": True, "count": 0}
    rows = temp_db.get_external_items_for_window(
        out["window"]["start"], out["window"]["end"]
    )
    assert len(rows) == 1


def test_one_source_failure_does_not_block_others(temp_db):
    good = _ok_source("youtube_video", [
        ExternalItem("youtube_video", "v1", "2026-05-10", "", "t", "u", "scheduled", "{}")
    ])
    bad = _bad_source("simplecast", SessionExpiredError("expired"))
    out = run_refresh(sources=[good, bad], window_days_back=30, window_days_forward=60)
    assert out["results"]["youtube_video"]["ok"] is True
    assert out["results"]["simplecast"]["ok"] is False
    assert "expired" in out["results"]["simplecast"]["error"]


def test_concurrent_refresh_returns_busy(temp_db):
    """A second call while one is running yields a 'busy' marker."""
    import threading
    started = threading.Event()
    release = threading.Event()

    class Slow:
        NAME = "youtube_video"
        PLATFORMS = ["youtube_video"]
        @staticmethod
        def fetch(window_start, window_end):
            started.set()
            release.wait(timeout=5)
            return []

    result_holder = {}
    def first():
        result_holder["first"] = run_refresh(sources=[Slow], window_days_back=1, window_days_forward=1)

    t = threading.Thread(target=first)
    t.start()
    assert started.wait(timeout=3), "first run never started"
    second = run_refresh(sources=[Slow], window_days_back=1, window_days_forward=1)
    assert second == {"busy": True}
    release.set()
    t.join(timeout=5)
    assert result_holder["first"]["results"]["youtube_video"]["ok"] is True


def test_failed_source_does_not_mark_stale(temp_db):
    """Pre-existing rows for a failed source must keep status='active' (or scheduled/published)."""
    temp_db.upsert_external_items([{
        "platform": "simplecast", "external_id": "uuid1", "iso_date": "2026-05-10",
        "scheduled_time": "", "title": "old", "url": "u",
        "status": "scheduled", "raw_json": "{}",
    }])
    bad = _bad_source("simplecast", RuntimeError("boom"))
    run_refresh(sources=[bad], window_days_back=30, window_days_forward=60)
    rows = temp_db.get_external_items_for_window("2026-04-01", "2026-06-30")
    assert any(r["external_id"] == "uuid1" and r["status"] != "deleted" for r in rows)


def test_successful_source_marks_missing_items_stale(temp_db):
    temp_db.upsert_external_items([{
        "platform": "youtube_video", "external_id": "old_vid", "iso_date": "2026-05-10",
        "scheduled_time": "", "title": "old", "url": "u",
        "status": "scheduled", "raw_json": "{}",
    }])
    src = _ok_source("youtube_video", [])  # nothing returned
    run_refresh(sources=[src], window_days_back=60, window_days_forward=60)
    rows = temp_db.get_external_items_for_window("2026-04-01", "2026-06-30")
    assert all(r["external_id"] != "old_vid" for r in rows)


def test_youtube_source_marks_both_platforms_stale(temp_db):
    """A source with PLATFORMS=['youtube_video','youtube_shorts'] should mark stale on both."""
    temp_db.upsert_external_items([
        {"platform": "youtube_video", "external_id": "old_v", "iso_date": "2026-05-10",
         "scheduled_time": "", "title": "ov", "url": "u", "status": "scheduled", "raw_json": "{}"},
        {"platform": "youtube_shorts", "external_id": "old_s", "iso_date": "2026-05-10",
         "scheduled_time": "", "title": "os", "url": "u", "status": "scheduled", "raw_json": "{}"},
    ])

    class YT:
        NAME = "youtube"
        PLATFORMS = ["youtube_video", "youtube_shorts"]
        @staticmethod
        def fetch(s, e):
            return []

    run_refresh(sources=[YT], window_days_back=60, window_days_forward=60)
    rows = temp_db.get_external_items_for_window("2026-04-01", "2026-06-30")
    assert all(r["external_id"] not in {"old_v", "old_s"} for r in rows)


def test_get_configured_sources_returns_all_modules():
    from core.calendar_refresh import get_configured_sources
    sources = get_configured_sources()
    names = sorted(s.NAME for s in sources)
    assert names == ["rock", "simplecast", "vista_social", "youtube"]
