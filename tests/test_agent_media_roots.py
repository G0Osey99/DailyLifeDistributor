"""Tests for agent.media_roots — the local-folder → scanner-kind bridge.

This is the feature that unblocks the agent upload path: before it, nothing
told the agent where local media lived, so scan() returned empty and every
agent-path upload failed file-not-found.
"""
from __future__ import annotations

import os

import pytest

from agent import media_roots as mr


# ---------------------------------------------------------------------------
# scan_roots_from_config — config keys → scanner kinds
# ---------------------------------------------------------------------------
def test_scan_roots_expands_thumbnail_to_both_kinds():
    """A single Thumbnails folder must feed BOTH 'thumbnail' and
    'short_thumbnail' — the dispatch reads short_thumbnail for the Shorts
    row, but the web path shares one thumbnails folder for video + Shorts."""
    out = mr.scan_roots_from_config({"thumbnail": "/m/thumbs"})
    assert out == {"thumbnail": "/m/thumbs", "short_thumbnail": "/m/thumbs"}


def test_scan_roots_maps_each_category_to_dispatch_kind():
    saved = {
        "video": "/m/horiz",
        "short_video": "/m/short",
        "audio": "/m/pod",
        "thumbnail": "/m/thumb",
        "email_thumbnail": "/m/email",
    }
    out = mr.scan_roots_from_config(saved)
    # Exactly the kinds agent/run_batch._dispatch_upload reads.
    assert out == {
        "video": "/m/horiz",
        "short_video": "/m/short",
        "audio": "/m/pod",
        "thumbnail": "/m/thumb",
        "short_thumbnail": "/m/thumb",
        "email_thumbnail": "/m/email",
    }


def test_scan_roots_ignores_empty_and_unknown_keys():
    out = mr.scan_roots_from_config({"video": "", "bogus": "/x", "audio": "/a"})
    assert out == {"audio": "/a"}


def test_scan_roots_handles_none():
    assert mr.scan_roots_from_config(None) == {}


# ---------------------------------------------------------------------------
# autodetect_roots — one-click parent-folder detection
# ---------------------------------------------------------------------------
def test_autodetect_matches_subfolders_by_name(tmp_path):
    for sub in ("Horizontal Video", "Vertical Shorts", "Podcast Audio",
                "Thumbnails", "Email Thumbnails"):
        (tmp_path / sub).mkdir()
    out = mr.autodetect_roots(str(tmp_path))
    assert out["video"].endswith("Horizontal Video")
    assert out["short_video"].endswith("Vertical Shorts")
    assert out["audio"].endswith("Podcast Audio")
    assert out["email_thumbnail"].endswith("Email Thumbnails")
    # "Thumbnails" must NOT be stolen by the email rule, and "Email
    # Thumbnails" must NOT be classified as plain thumbnail.
    assert out["thumbnail"].endswith("Thumbnails")
    assert not out["thumbnail"].lower().endswith("email thumbnails")


def test_autodetect_skips_ambiguous_category(tmp_path):
    # Two folders both look like 'video' → ambiguous, leave unset.
    (tmp_path / "Horizontal A").mkdir()
    (tmp_path / "Landscape B").mkdir()
    out = mr.autodetect_roots(str(tmp_path))
    assert "video" not in out


def test_autodetect_ignores_files_and_unmatched(tmp_path):
    (tmp_path / "random.txt").write_text("x")
    (tmp_path / "Unrelated Folder").mkdir()
    (tmp_path / "Podcast").mkdir()
    out = mr.autodetect_roots(str(tmp_path))
    assert out == {"audio": str(tmp_path / "Podcast")}


def test_autodetect_missing_dir_returns_empty():
    assert mr.autodetect_roots("/no/such/dir/here") == {}


# ---------------------------------------------------------------------------
# apply_saved_roots / save_and_apply — config ↔ scanner wiring
# ---------------------------------------------------------------------------
def test_apply_saved_roots_pushes_into_scanner(monkeypatch):
    from agent import config, scan
    monkeypatch.setattr(config, "get_media_roots",
                        lambda: {"video": "/m/v", "audio": "/m/a"})
    captured = {}
    monkeypatch.setattr(scan, "set_roots", lambda r: captured.update(r))
    out = mr.apply_saved_roots()
    assert out == {"video": "/m/v", "audio": "/m/a"}
    assert captured == {"video": "/m/v", "audio": "/m/a"}


def test_save_and_apply_persists_clean_keys_then_applies(monkeypatch):
    from agent import config, scan
    stored = {}
    monkeypatch.setattr(config, "set_media_roots", lambda r: stored.update(r))
    monkeypatch.setattr(config, "get_media_roots", lambda: dict(stored))
    monkeypatch.setattr(scan, "set_roots", lambda r: None)
    # Mix in an empty value + a bogus key; both should be dropped on persist.
    mr.save_and_apply({"video": "/m/v", "audio": "", "bogus": "/x"})
    assert stored == {"video": "/m/v"}


def test_apply_saved_roots_safe_when_nothing_configured(monkeypatch):
    from agent import config, scan
    monkeypatch.setattr(config, "get_media_roots", lambda: {})
    monkeypatch.setattr(scan, "set_roots", lambda r: None)
    assert mr.apply_saved_roots() == {}
