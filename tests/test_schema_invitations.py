from core import db


def test_invitations_columns():
    db.init_db()
    with db._get_conn() as c:
        cols = {r[1] for r in c.execute(
            "PRAGMA table_info('invitations')").fetchall()}
    assert {"id", "org_id", "inviter_user_id", "email", "role",
            "token_hash", "expires_at", "accepted_at", "revoked_at",
            "created_at"} <= cols
