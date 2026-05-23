from core import db


def _cols(t):
    with db._get_conn() as c:
        return {r[1] for r in c.execute(f"PRAGMA table_info('{t}')").fetchall()}


def test_audit_log_columns():
    db.init_db()
    cols = _cols("audit_log")
    assert {"id", "org_id", "actor_user_id", "action",
            "target_type", "target_id", "metadata", "ip",
            "user_agent", "created_at"} <= cols


def test_audit_log_archive_mirrors():
    db.init_db()
    assert _cols("audit_log") == _cols("audit_log_archive")
