from core import db


def test_recovery_codes_columns():
    db.init_db()
    with db._get_conn() as c:
        cols = {r[1] for r in c.execute(
            "PRAGMA table_info('recovery_codes')").fetchall()}
    assert {"id", "user_id", "code_hash", "used_at", "created_at"} <= cols


def test_recovery_requests_columns():
    db.init_db()
    with db._get_conn() as c:
        cols = {r[1] for r in c.execute(
            "PRAGMA table_info('recovery_requests')").fetchall()}
    assert {"id", "user_id", "requested_at", "expires_at",
            "approver_user_id", "approved_at",
            "password_reset_token_hash", "consumed_at"} <= cols
