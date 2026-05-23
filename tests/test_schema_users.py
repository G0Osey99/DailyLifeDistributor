from core import db


def _cols(table: str) -> set[str]:
    with db._get_conn() as c:
        return {r[1] for r in c.execute(f"PRAGMA table_info('{table}')").fetchall()}


def test_users_table_columns():
    db.init_db()
    cols = _cols("users")
    assert {"id", "username", "email", "password_hash",
            "totp_secret_encrypted", "email_2fa_enabled",
            "program_owner", "created_at", "last_login_at",
            "password_changed_at"} <= cols


def test_users_username_email_unique():
    db.init_db()
    import sqlite3
    with db._get_conn() as c:
        c.execute("INSERT INTO users (username, email, password_hash, "
                  "email_2fa_enabled, program_owner, created_at) "
                  "VALUES ('a', 'a@x.com', 'h', 0, 0, '2026-01-01T00:00:00+00:00')")
        c.commit()
        try:
            c.execute("INSERT INTO users (username, email, password_hash, "
                      "email_2fa_enabled, program_owner, created_at) "
                      "VALUES ('a', 'b@x.com', 'h', 0, 0, '2026-01-01T00:00:00+00:00')")
            c.commit()
            dup_user = False
        except sqlite3.IntegrityError:
            dup_user = True
        try:
            c.execute("INSERT INTO users (username, email, password_hash, "
                      "email_2fa_enabled, program_owner, created_at) "
                      "VALUES ('b', 'a@x.com', 'h', 0, 0, '2026-01-01T00:00:00+00:00')")
            c.commit()
            dup_email = False
        except sqlite3.IntegrityError:
            dup_email = True
    assert dup_user and dup_email
