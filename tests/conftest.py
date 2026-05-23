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
def _legacy_password_enabled_for_tests(monkeypatch):
    """Multi-tenant phase α: opt every existing test into the legacy
    shared-password login form by default.

    Many existing tests post ``data={"password": "pw"}`` to /login. The new
    multi-tenant auth path requires ``username`` + ``password`` and would
    otherwise 401 every one of them. Tests for the new Argon2id login
    path explicitly set LEGACY_PASSWORD_ENABLED=false in their own fixtures.
    """
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "true")
    yield


@pytest.fixture(autouse=True)
def _disable_rate_limiting(monkeypatch):
    """Disable flask-limiter + the manual ws-connect counters in tests.

    Many existing tests hit /agent/* and /pair/* dozens of times in a single
    test run; with rate limiting on those would 429 partway through.
    Production keeps the default RATELIMIT_ENABLED=true; the test suite
    flips it off via RATELIMIT_ENABLED=false so create_app() reads it
    before initialising the Limiter.
    """
    monkeypatch.setenv("RATELIMIT_ENABLED", "false")
    yield


@pytest.fixture(autouse=True)
def _reset_relay_and_dispatch_singletons():
    """Clear process-wide relay state + dispatch job registry between tests.

    Two module-level singletons survive across tests in this suite:

      * ``blueprints.agent.RELAY`` — a single ``core.relay.Relay`` instance
        keyed by account ("default") that holds maps of online agents +
        browsers and their websocket sink callbacks. Tests that
        ``importlib.reload(blueprints.agent)`` get a *new* RELAY object,
        but tests that don't reload still see the old one — and the old
        one may still hold sink closures that point at already-closed
        sockets. Routing to a closed sink isn't fatal, but a stale agent
        registration makes ``GET /agent/devices/online`` (and any test
        that checks ``agent_online``) report ghosts.

      * ``core.agent_dispatch._jobs`` — keyed by ``job_id``, the per-job
        SSE queue + session_id. A previous test's job_id sitting in this
        dict won't cause routing to misfire (the relay e2e test even
        clears it on purpose to simulate a restart), but leaving it is
        memory-leak-y across hundreds of tests and confuses any new test
        that iterates the registry.

    We clear both before and after every test. Importing the modules is
    cheap (already loaded by 99% of tests); we tolerate ImportError for
    the rare test that runs without the agent stack installed.
    """
    def _clear() -> None:
        try:
            from blueprints import agent as _agent_bp
            _relay = _agent_bp.RELAY
            with _relay._lock:
                _relay._rooms.clear()
        except Exception:
            pass
        try:
            from core import agent_dispatch as _ad
            with _ad._jobs_lock:
                _ad._jobs.clear()
        except Exception:
            pass
        # Per-job cancel events live in core.upload_jobs (web-path cancel).
        # Stale entries leak across tests if we don't reset between them.
        try:
            from core import upload_jobs as _uj
            with _uj._JOBS_LOCK:
                _uj._jobs.clear()
                _uj._cancel_events.clear()
        except Exception:
            pass

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    """Provide a valid Fernet master key for every test.

    Crypto/secret-store code fails closed without SECRET_ENC_KEY; set a
    fixed test key so unit tests can encrypt/decrypt deterministically.
    """
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SECRET_ENC_KEY", Fernet.generate_key().decode())
    yield


# ---------- Multi-tenant phase β: role-scoped client fixtures ----------

@pytest.fixture
def app(monkeypatch):
    """Real Flask app via create_app(), with the per-test DB already isolated."""
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "false")
    monkeypatch.setenv("RATELIMIT_ENABLED", "false")
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
    # SESSION_COOKIE_SECURE off so the test client uses cookies.
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    # The factory mints a random FLASK_SECRET_KEY when none is set; pin
    # one so test_client.session_transaction is stable across calls.
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-key-for-phase-beta")
    from app import create_app
    a = create_app()
    a.config["TESTING"] = True
    yield a


@pytest.fixture
def client(app):
    return app.test_client()


def _ensure_org(oid: int, name: str | None = None):
    from core import db
    with db._get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO organizations "
            "(id, name, slug, plan, created_at) "
            "VALUES (?, ?, ?, 'free', datetime('now'))",
            (oid, name or f"Test Org {oid}", f"test-org-{oid}"),
        )
        c.commit()


def _make_role_client(app, role: str, *, oid: int = 1, suffix: str = ""):
    """Insert a user + membership, prime the session on a *fresh* test client.

    Each call returns a NEW test_client instance so two role fixtures in
    the same test (e.g. client_owner + client_manager) don't trample each
    other's session cookies.
    """
    from core import db, user_store
    _ensure_org(oid)
    tag = f"{role}{suffix}_o{oid}"
    user = user_store.create_user(
        username=tag, email=f"{tag}@example.com",
        password="long-enough-pw-12!",
    )
    user_store.update_password(user["id"], "long-enough-pw-12!")
    with db._get_conn() as c:
        c.execute(
            "INSERT INTO org_memberships "
            "(user_id, org_id, role, joined_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (user["id"], oid, role),
        )
        c.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = user["id"]
        s["current_org_id"] = oid
    return c


@pytest.fixture
def client_owner(app):
    return _make_role_client(app, "owner")


@pytest.fixture
def client_manager(app):
    return _make_role_client(app, "manager")


@pytest.fixture
def client_user(app):
    return _make_role_client(app, "user")


@pytest.fixture
def client_owner_b(app):
    """Owner of org_id=2 (cross-org isolation tests)."""
    return _make_role_client(app, "owner", oid=2, suffix="_b")
