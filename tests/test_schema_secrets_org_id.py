from core import db


def test_secrets_has_org_id():
    db.init_db()
    with db._get_conn() as c:
        cols = {r[1] for r in c.execute(
            "PRAGMA table_info('secrets')").fetchall()}
    assert "org_id" in cols


def test_legacy_secret_rows_have_null_org_id():
    db.init_db()
    with db._get_conn() as c:
        c.execute("INSERT INTO secrets (name, kind, value, updated_at) "
                  "VALUES ('k', 'str', X'00', '2026-01-01T00:00:00+00:00')")
        c.commit()
        row = c.execute("SELECT org_id FROM secrets WHERE name='k'").fetchone()
    assert row["org_id"] is None
