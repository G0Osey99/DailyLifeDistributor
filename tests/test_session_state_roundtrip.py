"""Round-trip tests for SessionState serialization.

The state dict is what `state.db` persists, so a regression here corrupts
the resume flow silently. We exercise:

- UploadElements.to_dict ↔ UploadElements(**...) introspection path
- ReviewEntry → dict → ReviewEntry via SessionState helpers
- Full SessionState → JSON → SessionState (via DB save+load)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from core.session_state import ReviewEntry, SessionState, UploadElements, infer_wistia_ref


def _make_entry() -> ReviewEntry:
    return ReviewEntry(
        date="2026-04-29",
        display_date="April 29, 2026",
        youtube_video_path="/x/yt.mp4",
        youtube_shorts_path="/x/yt_260429.mp4",
        podcast_path="/x/p.mp3",
        thumbnail_path="/x/t.jpg",
        youtube_title="Title",
        youtube_shorts_title="Shorts Title",
        podcast_title="Podcast Title",
        description="Body",
        tags=["a", "b"],
        passage="Acts 1:1",
        scripture="text – Acts 1:1",
        episode_title="Ep",
        prayer="pray",
        topic_hint="hope",
        wistia_ref="app 260429",
        youtube_schedule_dt=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
        shorts_schedule_dt=datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
        podcast_schedule_dt=datetime(2026, 4, 29, 6, 0, tzinfo=timezone.utc),
        vista_schedule_dt=datetime(2026, 4, 29, 12, 30, tzinfo=timezone.utc),
        vista_caption="caption",
        platforms_enabled={"youtube_video": True, "rock": False},
        elements=UploadElements(
            yt_video_thumbnail=False,  # non-default
            rock_image=False,           # non-default
        ),
    )


def test_upload_elements_roundtrip_preserves_non_defaults():
    elems = UploadElements(yt_video_thumbnail=False, sc_schedule=False)
    d = elems.to_dict()
    # Field we changed survives.
    assert d["yt_video_thumbnail"] is False
    assert d["sc_schedule"] is False
    # Unchanged field stays at its default.
    assert d["rock_enabled"] is True
    # Round-trip via the same introspection path used by _entry_from_dict.
    rebuilt = UploadElements(**{k: v for k, v in d.items()})
    assert rebuilt == elems


def test_to_dict_includes_every_field():
    """Catches the case where someone adds a dataclass field but forgets the dict."""
    elems = UploadElements()
    d = elems.to_dict()
    assert set(d.keys()) == set(UploadElements.__dataclass_fields__.keys())


def test_review_entry_roundtrip(temp_db):
    """Full SessionState save/load round-trip restores entry fields."""
    s = SessionState()
    entry = _make_entry()
    s.entries[entry.date] = entry
    s.selected_dates = [entry.date]
    s.flush_pending_save()  # force write rather than wait on debounce

    loaded = SessionState.load(s.session_id)
    assert loaded is not None
    assert loaded.selected_dates == [entry.date]
    got = loaded.entries[entry.date]
    assert got.youtube_title == entry.youtube_title
    assert got.tags == entry.tags
    assert got.elements.yt_video_thumbnail is False
    assert got.elements.rock_image is False
    assert got.elements.rock_enabled is True
    assert got.youtube_schedule_dt == entry.youtube_schedule_dt
    assert got.platforms_enabled == entry.platforms_enabled


def test_resume_latest_returns_in_progress(temp_db):
    s = SessionState()
    s.entries["2026-04-29"] = _make_entry()
    s.selected_dates = ["2026-04-29"]
    s.flush_pending_save()

    resumed = SessionState.resume_latest()
    assert resumed is not None
    assert resumed.session_id == s.session_id


def test_infer_wistia_ref_picks_first_six_digit_run():
    assert infer_wistia_ref("/path/to/clip_260429_v2.mp4") == "app 260429"
    assert infer_wistia_ref("no_digits.mp4") == ""
    assert infer_wistia_ref(None) == ""
    # 8-digit run shouldn't match (the negative lookarounds require the
    # 6-digit window to NOT be embedded in a longer digit run).
    assert infer_wistia_ref("clip_20250429.mp4") == ""
