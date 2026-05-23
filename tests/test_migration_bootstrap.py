import pytest
from core import migration_bootstrap, user_store, org_store, db


def test_run_migration_creates_lcbc_org(monkeypatch):
    monkeypatch.setenv("PROGRAM_OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "bootstrappw12345")
    migration_bootstrap.run_migration()
    org = org_store.get_org_by_slug("lcbc-church")
    assert org is not None
    assert org["name"] == "LCBC Church"


def test_run_migration_creates_bootstrap_user(monkeypatch):
    monkeypatch.setenv("PROGRAM_OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "bootstrappw12345")
    migration_bootstrap.run_migration()
    u = user_store.get_user_by_email("owner@example.com")
    assert u is not None
    assert u["program_owner"] == 1
    # Forced change on first login.
    assert u["password_changed_at"] is None


def test_run_migration_is_idempotent(monkeypatch):
    monkeypatch.setenv("PROGRAM_OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "bootstrappw12345")
    migration_bootstrap.run_migration()
    migration_bootstrap.run_migration()
    migration_bootstrap.run_migration()
    orgs = [o for o in org_store.list_orgs() if o["slug"] == "lcbc-church"]
    assert len(orgs) == 1
    with db._get_conn() as c:
        (n,) = c.execute(
            "SELECT COUNT(*) FROM users WHERE email='owner@example.com'"
        ).fetchone()
    assert n == 1


def test_run_migration_backfills_existing_devices_secrets_history(monkeypatch):
    # Pre-seed legacy rows BEFORE migration.
    with db._get_conn() as c:
        c.execute("INSERT INTO agent_devices (id, name, token_hash, created_at) "
                  "VALUES ('legacy-d1', 'D1', 'h', '2026-01-01T00:00:00+00:00')")
        c.execute("INSERT INTO secrets (name, kind, value, updated_at) "
                  "VALUES ('legacy.k', 'str', X'00', '2026-01-01T00:00:00+00:00')")
        c.execute("INSERT INTO upload_history (session_id, uploaded_at, iso_date, "
                  "platform, title, file_path, success, url, scheduled_time, error) "
                  "VALUES ('s', '2026-01-01T00:00:00+00:00', '2026-01-01', 'YouTube Video', "
                  "'t', '/tmp/a', 1, 'u', '', '')")
        c.commit()
    monkeypatch.setenv("PROGRAM_OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "bootstrappw12345")
    migration_bootstrap.run_migration()
    org = org_store.get_org_by_slug("lcbc-church")
    user = user_store.get_user_by_email("owner@example.com")
    with db._get_conn() as c:
        (d_user,) = c.execute(
            "SELECT user_id FROM agent_devices WHERE id='legacy-d1'"
        ).fetchone()
        (s_org,) = c.execute(
            "SELECT org_id FROM secrets WHERE name='legacy.k'"
        ).fetchone()
        h_row = c.execute(
            "SELECT org_id, user_id FROM upload_history WHERE session_id='s'"
        ).fetchone()
    assert d_user == user["id"]
    assert s_org == org["id"]
    assert h_row["org_id"] == org["id"]
    assert h_row["user_id"] == user["id"]


def test_run_migration_aborts_without_program_owner_email(monkeypatch):
    monkeypatch.delenv("PROGRAM_OWNER_EMAIL", raising=False)
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "anything12345")
    with pytest.raises(migration_bootstrap.MigrationAborted):
        migration_bootstrap.run_migration()
