import pytest


def test_create_app_runs_migration_when_env_present(monkeypatch):
    monkeypatch.setenv("PROGRAM_OWNER_EMAIL", "boot@example.com")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "bootstrappw12345")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import importlib
    import app as flask_app_module
    importlib.reload(flask_app_module)
    from core import org_store, user_store
    assert org_store.get_org_by_slug("lcbc-church") is not None
    assert user_store.get_user_by_email("boot@example.com") is not None


def test_create_app_swallows_migration_abort_when_no_bootstrap(monkeypatch, caplog):
    """Re-running create_app() after a successful migration must not crash even if PROGRAM_OWNER_EMAIL was unset on a later boot."""
    monkeypatch.setenv("PROGRAM_OWNER_EMAIL", "boot2@example.com")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "bootstrappw12345")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import importlib
    import app as flask_app_module
    importlib.reload(flask_app_module)
    monkeypatch.delenv("PROGRAM_OWNER_EMAIL", raising=False)
    # Second create_app() must not raise — the program-owner row already
    # exists, so MigrationAborted should NOT be raised.
    importlib.reload(flask_app_module)
