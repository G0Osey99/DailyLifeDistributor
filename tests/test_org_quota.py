"""Per-org YouTube quota tracking (phase δ).

yt_quota_usage table + track_org_quota_usage / get_org_quota_used helpers.
Same QUOTA_COSTS table; same Pacific-day reset key.
"""
from core import db, quota


def test_yt_quota_usage_table_exists():
    db.init_db()
    with db._get_conn() as c:
        row = c.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='yt_quota_usage'"
        ).fetchone()
        assert row is not None


def test_track_increments_org_usage():
    db.init_db()
    quota.track_org_quota_usage(org_id=1, action="video_upload")
    assert quota.get_org_quota_used(1) == 1600
    quota.track_org_quota_usage(org_id=1, action="thumbnail_set")
    assert quota.get_org_quota_used(1) == 1650


def test_different_orgs_isolated():
    db.init_db()
    quota.track_org_quota_usage(org_id=1, action="video_upload")
    quota.track_org_quota_usage(org_id=2, action="thumbnail_set")
    assert quota.get_org_quota_used(1) == 1600
    assert quota.get_org_quota_used(2) == 50


def test_unknown_action_with_no_units_noop():
    db.init_db()
    quota.track_org_quota_usage(org_id=1, action="nonexistent")
    assert quota.get_org_quota_used(1) == 0


def test_explicit_units_overrides_lookup():
    db.init_db()
    quota.track_org_quota_usage(org_id=1, action="custom", units=42)
    assert quota.get_org_quota_used(1) == 42
