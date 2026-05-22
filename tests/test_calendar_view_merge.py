"""Test the dedupe merge logic used by /calendar."""
from core.calendar_refresh_view import merge_for_window


def _hist(platform, external_id, iso_date, **kw):
    return {
        "platform": platform, "external_id": external_id, "iso_date": iso_date,
        "title": kw.get("title", "h"), "url": kw.get("url", ""),
        "scheduled_time": kw.get("scheduled_time", ""),
        "success": kw.get("success", 1), "error": kw.get("error", ""),
        "file_path": kw.get("file_path", ""), "id": kw.get("id", 1),
    }


def _ext(platform, external_id, iso_date, **kw):
    return {
        "platform": platform, "external_id": external_id, "iso_date": iso_date,
        "title": kw.get("title", "e"), "url": kw.get("url", ""),
        "scheduled_time": kw.get("scheduled_time", ""),
        "status": kw.get("status", "scheduled"), "id": kw.get("id", 1),
    }


def test_external_with_matching_id_is_suppressed():
    history = [_hist("youtube_video", "vid1", "2026-05-10", title="from_app")]
    external = [_ext("youtube_video", "vid1", "2026-05-10", title="from_scrape")]
    merged = merge_for_window(history, external)
    assert len(merged) == 1
    assert merged[0]["title"] == "from_app"
    assert merged[0]["source"] == "upload"


def test_external_with_unmatched_id_is_kept():
    history = []
    external = [_ext("youtube_video", "new1", "2026-05-10")]
    merged = merge_for_window(history, external)
    assert len(merged) == 1
    assert merged[0]["source"] == "external"


def test_history_with_null_external_id_does_not_swallow_external_rows():
    history = [_hist("youtube_video", None, "2026-05-10")]
    external = [_ext("youtube_video", "real_id", "2026-05-10")]
    merged = merge_for_window(history, external)
    assert len(merged) == 2


def test_different_platforms_with_same_id_dont_collide():
    history = [_hist("youtube_video", "abc", "2026-05-10")]
    external = [_ext("rock", "abc", "2026-05-10")]
    merged = merge_for_window(history, external)
    assert len(merged) == 2


# ---------------------------------------------------------------------------
# Stale-failure suppression — a failed history row is hidden when an external
# row covers the same (provider, iso_date), because the user has fixed the
# upload manually on the platform.
# ---------------------------------------------------------------------------

def test_failed_history_suppressed_when_external_covers_same_date():
    """User's failed upload is hidden once the platform has the item scheduled."""
    history = [_hist(
        "youtube_video", "", "2026-05-10",
        success=0, error="quota exceeded", title="failed_attempt",
    )]
    external = [_ext("youtube_video", "manually_scheduled", "2026-05-10",
                     title="from_scrape")]
    merged = merge_for_window(history, external)
    assert len(merged) == 1
    assert merged[0]["source"] == "external"
    assert merged[0]["title"] == "from_scrape"


def test_failed_history_suppressed_even_when_external_id_differs():
    """Manual reschedule produces a different external_id; suppression still wins."""
    history = [_hist(
        "youtube_video", "vid_old_failed", "2026-05-10",
        success=0, error="auth lost",
    )]
    external = [_ext("youtube_video", "vid_new_manual", "2026-05-10")]
    merged = merge_for_window(history, external)
    assert len(merged) == 1
    assert merged[0]["source"] == "external"
    assert merged[0]["external_id"] == "vid_new_manual"


def test_successful_history_not_suppressed_by_external():
    """A successful upload is still kept; the external row matching its id is dropped."""
    history = [_hist("youtube_video", "vid1", "2026-05-10",
                     success=1, error="", title="from_app")]
    external = [_ext("youtube_video", "vid1", "2026-05-10")]
    merged = merge_for_window(history, external)
    assert len(merged) == 1
    assert merged[0]["source"] == "upload"


def test_failure_with_no_external_in_bucket_is_kept():
    """Genuine failures with no platform fix should remain visible."""
    history = [_hist("youtube_video", "", "2026-05-10",
                     success=0, error="network timeout")]
    merged = merge_for_window(history, [])
    assert len(merged) == 1
    assert merged[0]["source"] == "upload"
    assert merged[0]["error"] == "network timeout"


def test_failure_in_one_provider_not_suppressed_by_external_in_another():
    """Cross-provider externals don't suppress unrelated failures on the same date."""
    history = [_hist("youtube_video", "", "2026-05-10",
                     success=0, error="oops")]
    external = [_ext("rock", "rock_item", "2026-05-10")]
    merged = merge_for_window(history, external)
    # Both kept: failure for youtube + external for rock.
    assert len(merged) == 2


def test_published_external_beats_scheduled_history_same_id():
    """Same content: scheduled in history, now published on the platform.
    The published row wins, collapsed to one item — the day-over-day transition.
    """
    h = _hist("youtube_video", "vidX", "2026-05-10", title="sched")
    h["_status"] = "scheduled"
    external = [_ext("youtube_video", "vidX", "2026-05-10",
                     status="published", title="live")]
    merged = merge_for_window([h], external)
    assert len(merged) == 1
    assert merged[0]["source"] == "external"
    assert merged[0]["title"] == "live"


def test_published_history_beats_scheduled_external_same_id():
    h = _hist("youtube_video", "vidY", "2026-05-10", title="live")
    h["_status"] = "published"
    external = [_ext("youtube_video", "vidY", "2026-05-10", status="scheduled")]
    merged = merge_for_window([h], external)
    assert len(merged) == 1
    assert merged[0]["source"] == "upload"
    assert merged[0]["title"] == "live"


def test_scheduled_then_published_keeps_total_count_stable():
    """Two distinct posts: one already published, one still scheduled. The
    scheduled one's later 'published' refresh must not add a row — total stays
    the same, scheduled count drops by one as published rises."""
    # Day 1: post A published, post B scheduled.
    day1 = merge_for_window(
        [],
        [_ext("simplecast", "A", "2026-05-10", status="published"),
         _ext("simplecast", "B", "2026-05-11", status="scheduled")],
    )
    assert len(day1) == 2
    # Day 2: B has now published (same id, status flips). Still two items.
    day2 = merge_for_window(
        [],
        [_ext("simplecast", "A", "2026-05-10", status="published"),
         _ext("simplecast", "B", "2026-05-11", status="published")],
    )
    assert len(day2) == 2
    assert sum(1 for r in day2 if (r.get("_status") or r.get("status")) == "published") == 2


def test_status_field_recognized_as_failure_signal():
    """Calendar pre-decorates rows with `_status='failed'` instead of `success`."""
    history = [{
        "platform": "youtube_video", "external_id": "", "iso_date": "2026-05-10",
        "_status": "failed", "error": "boom", "title": "stale",
    }]
    external = [_ext("youtube_video", "live_id", "2026-05-10")]
    merged = merge_for_window(history, external)
    assert len(merged) == 1
    assert merged[0]["source"] == "external"
