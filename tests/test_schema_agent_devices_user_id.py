from core import db


def test_agent_devices_has_user_id():
    db.init_db()
    with db._get_conn() as c:
        cols = {r[1] for r in c.execute(
            "PRAGMA table_info('agent_devices')").fetchall()}
    assert "user_id" in cols


def test_legacy_rows_keep_null_user_id():
    db.init_db()
    with db._get_conn() as c:
        c.execute("INSERT INTO agent_devices (id, name, token_hash, created_at) "
                  "VALUES ('d1', 'D', 'h', '2026-01-01T00:00:00+00:00')")
        c.commit()
        row = c.execute("SELECT user_id FROM agent_devices WHERE id='d1'").fetchone()
    assert row["user_id"] is None
