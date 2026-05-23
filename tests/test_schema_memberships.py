import sqlite3
from core import db


def test_memberships_columns():
    db.init_db()
    with db._get_conn() as c:
        cols = {r[1] for r in c.execute(
            "PRAGMA table_info('org_memberships')").fetchall()}
    assert {"id", "user_id", "org_id", "role", "joined_at"} <= cols


def test_memberships_unique_user_org():
    db.init_db()
    with db._get_conn() as c:
        c.execute("INSERT INTO organizations (name, slug, plan, require_2fa, created_at) "
                  "VALUES ('O', 'o', 'free', 0, '2026-01-01T00:00:00+00:00')")
        c.execute("INSERT INTO users (username, email, password_hash, "
                  "email_2fa_enabled, program_owner, created_at) "
                  "VALUES ('u', 'u@x.com', 'h', 0, 0, '2026-01-01T00:00:00+00:00')")
        c.commit()
        c.execute("INSERT INTO org_memberships (user_id, org_id, role, joined_at) "
                  "VALUES (1, 1, 'owner', '2026-01-01T00:00:00+00:00')")
        c.commit()
        try:
            c.execute("INSERT INTO org_memberships (user_id, org_id, role, joined_at) "
                      "VALUES (1, 1, 'user', '2026-01-01T00:00:00+00:00')")
            c.commit()
            raised = False
        except sqlite3.IntegrityError:
            raised = True
    assert raised


def test_memberships_role_check():
    db.init_db()
    with db._get_conn() as c:
        c.execute("INSERT INTO organizations (name, slug, plan, require_2fa, created_at) "
                  "VALUES ('O', 'o', 'free', 0, '2026-01-01T00:00:00+00:00')")
        c.execute("INSERT INTO users (username, email, password_hash, "
                  "email_2fa_enabled, program_owner, created_at) "
                  "VALUES ('u', 'u@x.com', 'h', 0, 0, '2026-01-01T00:00:00+00:00')")
        c.commit()
        try:
            c.execute("INSERT INTO org_memberships (user_id, org_id, role, joined_at) "
                      "VALUES (1, 1, 'superadmin', '2026-01-01T00:00:00+00:00')")
            c.commit()
            raised = False
        except sqlite3.IntegrityError:
            raised = True
    assert raised
