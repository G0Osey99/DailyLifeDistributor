"""platform_locks table — schema test (phase δ).

Per-org per-platform mutex for the web upload path so two members of the
same org don't trample each other on YouTube / Rock / SimpleCast / Vista.
"""
from core import db


def test_platform_locks_table_exists():
    db.init_db()
    with db._get_conn() as c:
        row = c.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='platform_locks'"
        ).fetchone()
        assert row is not None


def test_platform_locks_columns_present():
    db.init_db()
    with db._get_conn() as c:
        cols = {r[1] for r in c.execute(
            "PRAGMA table_info('platform_locks')").fetchall()}
    assert {"org_id", "platform", "locked_by_user_id",
            "locked_at", "expires_at"} <= cols


def test_platform_locks_primary_key_is_org_platform():
    """Primary key (org_id, platform) — only one holder per org+platform."""
    db.init_db()
    with db._get_conn() as c:
        c.execute(
            "INSERT INTO platform_locks "
            "(org_id, platform, locked_by_user_id, locked_at, expires_at) "
            "VALUES (1, 'youtube', 10, 'now', 'later')"
        )
        # Inserting the same (org, platform) again should fail.
        import sqlite3
        try:
            c.execute(
                "INSERT INTO platform_locks "
                "(org_id, platform, locked_by_user_id, locked_at, expires_at) "
                "VALUES (1, 'youtube', 11, 'now', 'later')"
            )
            assert False, "duplicate (org, platform) should have raised"
        except sqlite3.IntegrityError:
            pass


def test_init_db_is_idempotent_for_platform_locks():
    db.init_db()
    db.init_db()  # second call must not raise
