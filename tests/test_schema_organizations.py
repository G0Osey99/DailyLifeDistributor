"""organizations table schema + idempotent migration."""
import sqlite3
from core import db


def _cols(table: str) -> set[str]:
    with db._get_conn() as c:
        return {r[1] for r in c.execute(f"PRAGMA table_info('{table}')").fetchall()}


def test_organizations_table_created():
    db.init_db()
    with db._get_conn() as c:
        names = {
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "organizations" in names


def test_organizations_has_required_columns():
    db.init_db()
    cols = _cols("organizations")
    assert {"id", "name", "slug", "plan", "billing_email",
            "require_2fa", "created_at", "created_by_user_id",
            "disabled_at"} <= cols


def test_organizations_slug_unique():
    db.init_db()
    with db._get_conn() as c:
        c.execute("INSERT INTO organizations (name, slug, plan, require_2fa, created_at) "
                  "VALUES ('A', 'a', 'free', 0, '2026-01-01T00:00:00+00:00')")
        c.commit()
        try:
            c.execute("INSERT INTO organizations (name, slug, plan, require_2fa, created_at) "
                      "VALUES ('B', 'a', 'free', 0, '2026-01-01T00:00:00+00:00')")
            c.commit()
            raised = False
        except sqlite3.IntegrityError:
            raised = True
    assert raised


def test_init_db_is_idempotent():
    db.init_db()
    db.init_db()  # second call must not raise
