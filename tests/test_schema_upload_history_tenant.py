from core import db


def test_upload_history_has_tenant_columns():
    db.init_db()
    with db._get_conn() as c:
        cols = {r[1] for r in c.execute(
            "PRAGMA table_info('upload_history')").fetchall()}
    assert "org_id" in cols
    assert "user_id" in cols


def test_legacy_record_upload_still_works(temp_db):
    db.init_db()
    db.record_upload(
        session_id="s1", iso_date="2026-01-01", platform="YouTube Video",
        title="t", file_path="/tmp/a", success=True,
        url="https://youtube.com/watch?v=abc", scheduled_time="",
        error="",
    )
    with db._get_conn() as c:
        row = c.execute(
            "SELECT org_id, user_id FROM upload_history WHERE session_id='s1'"
        ).fetchone()
    assert row["org_id"] is None
    assert row["user_id"] is None
