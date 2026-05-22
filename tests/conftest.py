"""Shared pytest fixtures."""
import os
import pytest

# Browser-driven tests (Playwright / x11vnc / streamed remote-login) need a
# real display + browser and HANG in a headless CI or dev box, so they're not
# collected by default. Opt in with RUN_BROWSER_TESTS=1 on a machine that has
# them. This is what makes a plain `pytest` fully runnable everywhere.
_BROWSER_TEST_FILES = [
    "test_playwright_session.py",
    "test_playwright_session_secret.py",
    "test_remote_login.py",
    "test_remote_login_expiry.py",
    "test_remote_login_playwright.py",
    "test_remote_login_routes.py",
    "test_vnc.py",
]
if not os.environ.get("RUN_BROWSER_TESTS"):
    collect_ignore = list(_BROWSER_TEST_FILES)


@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch):
    """Redirect every state.db reference to a per-test temp file.

    Without this, any test that touches `core.db`, `core.quota`, or the
    Flask app indirectly writes to the developer's real `state.db` —
    polluting upload_history with fake rows and the YouTube quota
    counter with nonsense. Autouse + project-wide so even tests that
    forget to request `temp_db` still see isolation.
    """
    db_path = str(tmp_path / "isolated_state.db")
    from core import db as _db
    from core import quota as _quota
    monkeypatch.setattr(_db, "_DB_PATH", db_path)
    monkeypatch.setattr(_quota, "_DB_PATH", db_path)
    # Reset the one-shot PRAGMA flag so init_db() re-applies WAL on the
    # new file rather than skipping it.
    monkeypatch.setattr(_db, "_PRAGMAS_APPLIED", False, raising=False)
    # Create the schema on the temp DB. Code paths that reach the encrypted
    # secret store (uploaders reading creds, etc.) query the `secrets` table,
    # so every isolated test needs the tables present — not just those that
    # explicitly request the `temp_db` fixture.
    _db.init_db()
    yield


@pytest.fixture
def temp_db(_isolate_state_db):
    """Initialised core.db backed by the autouse temp file.

    Kept for tests that explicitly want the module handle (rather than
    relying on the autouse redirection alone).
    """
    from core import db as _db
    _db.init_db()
    yield _db


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Redirect config.yaml reads/writes to a per-test temp copy.

    Both write sites in blueprints/settings.py reference ``core_config.CONFIG_PATH``
    (the module attribute looked up at call time), so patching ``core.config.CONFIG_PATH``
    alone is sufficient to redirect every read and write to the temp copy.
    """
    import shutil
    from core import config as _config

    real = _config.CONFIG_PATH
    tmp_cfg = str(tmp_path / "config.yaml")
    try:
        shutil.copyfile(real, tmp_cfg)
    except OSError:
        open(tmp_cfg, "w").close()

    monkeypatch.setattr(_config, "CONFIG_PATH", tmp_cfg)
    # Invalidate the mtime-keyed in-process cache so load_config() re-reads
    # from the temp copy rather than returning a stale cached dict.
    _config.invalidate_config_cache()
    yield
    _config.invalidate_config_cache()


@pytest.fixture(autouse=True)
def _reset_circuit_breakers():
    """Clear the process-wide circuit-breaker registry around every test.

    Breakers persist in a module-level registry by design (a platform that
    fails repeatedly stays tripped across batches in a real run). In tests
    that state would leak between cases — a test that trips ``upload:X`` would
    short-circuit a later test for the same platform. Reset before and after.
    """
    from core import circuit_breaker
    circuit_breaker.reset_all()
    yield
    circuit_breaker.reset_all()


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    """Provide a valid Fernet master key for every test.

    Crypto/secret-store code fails closed without SECRET_ENC_KEY; set a
    fixed test key so unit tests can encrypt/decrypt deterministically.
    """
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SECRET_ENC_KEY", Fernet.generate_key().decode())
    yield
