"""Filename-only date scan tests for core.file_scanner.parse_names.

Mirrors the date-format coverage of test_file_scanner_dates.py but via the
name-list API the browser uses (it sends filenames, no filesystem access).
"""
from __future__ import annotations

from core.file_scanner import parse_names


def test_parse_names_groups_by_iso_date():
    names = ["DailyLife_250521.mp4", "DailyLife_250522.mp4", "notes.txt"]
    out = parse_names(names)            # -> {iso_date: [filename, ...]}
    assert "2025-05-21" in out
    assert "DailyLife_250521.mp4" in out["2025-05-21"]
    assert "2025-05-22" in out
    assert "DailyLife_250522.mp4" in out["2025-05-22"]
    assert "notes.txt" not in str(out)  # non-media / undated ignored


def test_parse_names_ignores_non_media_extensions():
    out = parse_names([".DS_Store", "thumbs_250521.txt", "real_250521.mp4"])
    assert out == {"2025-05-21": ["real_250521.mp4"]}


def test_parse_names_six_digit_ambiguity_surfaces_both_dates():
    # 240625 is ambiguous: YYMMDD = 2024-06-25, DDMMYY = 2025-06-24 (both
    # plausible). An ambiguous file shows up under each candidate date so the
    # user can pick the right one — mirroring the directory scanner's
    # alternatives behaviour from test_file_scanner_dates.py.
    out = parse_names(["clip_240625.mp4"])
    assert "2024-06-25" in out
    assert "2025-06-24" in out
    assert out["2024-06-25"] == ["clip_240625.mp4"]
    assert out["2025-06-24"] == ["clip_240625.mp4"]


def test_parse_names_thumbnail_and_audio_extensions_allowed():
    out = parse_names(["thumb_250521.png", "episode_250521.mp3"])
    assert sorted(out["2025-05-21"]) == ["episode_250521.mp3", "thumb_250521.png"]
