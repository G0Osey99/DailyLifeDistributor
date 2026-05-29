"""Tests for the YouTube quota tracker in ``core.quota``.

Covers both the global counter (``track_quota_usage`` /
``get_quota_used``) and the per-org counter (``track_org_quota_usage`` /
``get_org_quota_used``). Per-org day-rollover semantics are exercised by
monkey-patching the Pacific-time key helper.
"""
from __future__ import annotations

from core import db, quota


# ---------------- global counter ----------------

def test_track_quota_usage_bumps_counter():
    db.init_db()
    assert quota.get_quota_used() == 0
    quota.track_quota_usage("video_upload")
    assert quota.get_quota_used() == quota.QUOTA_COSTS["video_upload"]


def test_track_quota_usage_accumulates_same_day():
    db.init_db()
    quota.track_quota_usage("video_upload")
    quota.track_quota_usage("thumbnail_set")
    # video_upload + thumbnail_set
    assert quota.get_quota_used() == quota.QUOTA_COSTS["video_upload"] + quota.QUOTA_COSTS["thumbnail_set"]


def test_track_quota_usage_unknown_action_noop():
    db.init_db()
    quota.track_quota_usage("does_not_exist")
    assert quota.get_quota_used() == 0


def test_explicit_units_override():
    db.init_db()
    quota.track_quota_usage("video_upload", units=7)
    assert quota.get_quota_used() == 7


# ---------------- per-org counter ----------------

def test_track_org_quota_usage_bumps_counter():
    db.init_db()
    quota.track_org_quota_usage(org_id=1, action="video_upload")
    assert quota.get_org_quota_used(1) == quota.QUOTA_COSTS["video_upload"]


def test_org_quota_isolated_per_org():
    db.init_db()
    quota.track_org_quota_usage(org_id=1, action="video_upload")
    quota.track_org_quota_usage(org_id=2, action="thumbnail_set")
    assert quota.get_org_quota_used(1) == quota.QUOTA_COSTS["video_upload"]
    assert quota.get_org_quota_used(2) == 50


def test_org_quota_day_rollover_starts_new_row(monkeypatch):
    """Crossing midnight Pacific starts a fresh row keyed by the new date."""
    db.init_db()

    # Day 1: write one video_upload.
    monkeypatch.setattr(quota, "_today_key", lambda: "2026-05-23")
    quota.track_org_quota_usage(org_id=1, action="video_upload")
    assert quota.get_org_quota_used(1) == quota.QUOTA_COSTS["video_upload"]

    # Day 2: today's reading starts at 0 (new (org_id, date) row).
    monkeypatch.setattr(quota, "_today_key", lambda: "2026-05-24")
    assert quota.get_org_quota_used(1) == 0
    quota.track_org_quota_usage(org_id=1, action="thumbnail_set")
    assert quota.get_org_quota_used(1) == 50

    # And the old day's row is still intact.
    monkeypatch.setattr(quota, "_today_key", lambda: "2026-05-23")
    assert quota.get_org_quota_used(1) == quota.QUOTA_COSTS["video_upload"]


def test_org_quota_unknown_action_noop():
    db.init_db()
    quota.track_org_quota_usage(org_id=1, action="nope")
    assert quota.get_org_quota_used(1) == 0
