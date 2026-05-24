# agent/tests/test_scan.py
"""Tests for agent/scan.py — B8 coverage audit + lock-in.

Audit summary (2026-05-22):
  Uploaders need these path kinds (from agent/run_batch._dispatch_upload):
    video           — YouTube Video, Rock, Vista Social
    thumbnail       — YouTube Video, Rock
    short_video     — YouTube Shorts
    short_thumbnail — YouTube Shorts
    audio           — Simplecast (entry.podcast_path)
    email_thumbnail — Rock Email (separate dir per CLAUDE.md)

  NOT path-based (no local file needed):
    Rock spotlight/vista/reflection — Rock orchestrator uses text fields
      (entry.prayer, entry.scripture, entry.passage) and gathers images
      via the Unsplash API at runtime; no local image directory.

  Conclusion: _KIND_MAP in scan.py covers all required kinds.
  These tests lock in that coverage so regressions surface loudly.
"""
from agent import scan as _scan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dir(base, name, filename):
    """Create a subdirectory and place one date-named file in it."""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_bytes(b"fake")
    return str(d)


# ---------------------------------------------------------------------------
# KIND_MAP completeness
# ---------------------------------------------------------------------------

def test_kind_map_covers_all_required_media_kinds():
    """Every kind the uploaders request must exist in _KIND_MAP."""
    required = {
        "video",
        "thumbnail",
        "short_video",
        "short_thumbnail",
        "audio",
        "email_thumbnail",
    }
    assert required.issubset(_scan._KIND_MAP.keys()), (
        f"Missing kinds in _KIND_MAP: {required - _scan._KIND_MAP.keys()}"
    )


# ---------------------------------------------------------------------------
# scan() — per-kind indexing
# ---------------------------------------------------------------------------

def test_scan_indexes_video_directory(tmp_path):
    roots = {"video": _make_dir(tmp_path, "video", "260522_episode.mp4")}
    result = _scan.scan(roots=roots)
    assert "2026-05-22" in result
    assert result["2026-05-22"]["video"].endswith("260522_episode.mp4")


def test_scan_indexes_thumbnail_directory(tmp_path):
    roots = {"thumbnail": _make_dir(tmp_path, "thumbnail", "260522_thumb.jpg")}
    result = _scan.scan(roots=roots)
    assert result["2026-05-22"]["thumbnail"].endswith("260522_thumb.jpg")


def test_scan_indexes_audio_directory(tmp_path):
    roots = {"audio": _make_dir(tmp_path, "audio", "260522_podcast.mp3")}
    result = _scan.scan(roots=roots)
    assert result["2026-05-22"]["audio"].endswith("260522_podcast.mp3")


def test_scan_indexes_email_thumbnail_directory(tmp_path):
    """Rock Email uses a SEPARATE email_thumbnail dir (per CLAUDE.md).
    This is the most-likely gap called out in the B8 plan task."""
    roots = {"email_thumbnail": _make_dir(tmp_path, "email_thumb", "260522_email.png")}
    result = _scan.scan(roots=roots)
    assert "2026-05-22" in result, "email_thumbnail dir should yield a date entry"
    assert "email_thumbnail" in result["2026-05-22"], (
        "email_thumbnail kind missing from scan result"
    )
    assert result["2026-05-22"]["email_thumbnail"].endswith("260522_email.png")


def test_scan_indexes_short_video_and_short_thumbnail(tmp_path):
    roots = {
        "short_video":     _make_dir(tmp_path, "sv",  "260522_short.mp4"),
        "short_thumbnail": _make_dir(tmp_path, "st",  "260522_sthumb.jpg"),
    }
    result = _scan.scan(roots=roots)
    assert result["2026-05-22"]["short_video"].endswith("260522_short.mp4")
    assert result["2026-05-22"]["short_thumbnail"].endswith("260522_sthumb.jpg")


def test_scan_multiple_kinds_same_date(tmp_path):
    """All kinds for the same date should coexist in a single dict."""
    roots = {
        "video":           _make_dir(tmp_path, "v",  "260522_ep.mp4"),
        "thumbnail":       _make_dir(tmp_path, "t",  "260522_thumb.jpg"),
        "audio":           _make_dir(tmp_path, "a",  "260522_pod.mp3"),
        "email_thumbnail": _make_dir(tmp_path, "et", "260522_email.png"),
    }
    result = _scan.scan(roots=roots)
    day = result.get("2026-05-22", {})
    assert set(day.keys()) == {"video", "thumbnail", "audio", "email_thumbnail"}


# ---------------------------------------------------------------------------
# latest_results() caching
# ---------------------------------------------------------------------------

def test_latest_results_empty_before_first_scan():
    # Ensure clean state — other tests may have primed the cache.
    with _scan._last_lock:
        _scan._last_results.clear()
    assert _scan.latest_results() == {}


def test_latest_results_returns_last_scan(tmp_path):
    with _scan._last_lock:
        _scan._last_results.clear()

    roots = {"video": _make_dir(tmp_path, "v", "260522_ep.mp4")}
    _scan.scan(roots=roots)
    cached = _scan.latest_results()
    assert "2026-05-22" in cached
    assert cached["2026-05-22"]["video"].endswith("260522_ep.mp4")


# ---------------------------------------------------------------------------
# set_roots / get_roots round-trip
# ---------------------------------------------------------------------------

def test_set_and_get_roots_round_trip(tmp_path):
    roots = {"video": str(tmp_path / "v"), "audio": str(tmp_path / "a")}
    _scan.set_roots(roots)
    assert _scan.get_roots() == roots
    # Clean up.
    _scan.set_roots({})
