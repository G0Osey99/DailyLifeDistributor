# Auth Gate + Encrypted Secret Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the loopback-only access guard with a single shared-credential login, and encrypt every secret (API keys, YouTube OAuth files, Playwright session cookies) at rest in `state.db` using an app-held Fernet master key.

**Architecture:** A new `core/crypto.py` wraps a Fernet master key loaded from the `SECRET_ENC_KEY` env var. `core/secrets_store.py` persists encrypted blobs in a new `secrets` table and can materialize file-based secrets to short-lived `0600` temp files. `core/auth.py` + `blueprints/auth.py` provide the shared-credential login; `app.py`'s `before_request` swaps loopback-restriction for an auth gate. Call sites for API keys, YouTube tokens, and Playwright sessions read from the store (with a temporary plaintext fallback), and a one-time importer migrates existing plaintext in.

**Tech Stack:** Python 3.11+, Flask, Werkzeug (password hashing — already a dependency), `cryptography` (new — Fernet), SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-05-21-auth-secret-store-design.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `requirements.txt` | Add `cryptography` | Modify |
| `core/crypto.py` | Load master key; `encrypt`/`decrypt`; `validate_master_key` | Create |
| `core/db.py` | Add `secrets` table to `init_db()` | Modify (`init_db`, ~line 130) |
| `core/secrets_store.py` | Encrypted KV + blob store; temp-file materialization | Create |
| `core/auth.py` | Shared-credential hash, verify, env bootstrap, per-IP lockout | Create |
| `blueprints/auth.py` | `GET/POST /login`, `POST /logout`, `login_required` | Create |
| `templates/login.html` | Login form | Create |
| `app.py` | Replace loopback guard with auth gate; cookie config; bootstrap + key validation; register `auth_bp` | Modify (`create_app`, lines 89-180) |
| `core/image_gatherer.py` | Read Unsplash/Pexels keys from store (env fallback) | Modify (lines ~218, ~278) |
| `uploaders/youtube_uploader.py` | Read/write `client_secrets.json` + `token.json` via the store | Modify (lines ~40-43, 127-232) |
| `core/playwright_session.py` | Materialize/persist `storage_state` via the store | Modify (`_new_context`, save paths) |
| `scripts/migrate_secrets.py` | One-time idempotent plaintext→store importer | Create |
| `tests/test_crypto.py` … etc. | Unit tests per component | Create |

A shared pytest fixture for the master key is added once in Task 1 and reused.

---

## Task 1: Add `cryptography` and the crypto core

**Files:**
- Modify: `requirements.txt`
- Create: `core/crypto.py`
- Create: `tests/test_crypto.py`
- Modify: `tests/conftest.py` (add a master-key fixture)

- [ ] **Step 1: Add the dependency**

Add this line to `requirements.txt` (after the existing `requests` line, keeping the version-pinned style):

```
cryptography>=42.0.0,<46
```

Install it:

Run: `pip install -r requirements.txt`
Expected: `cryptography` installs without error.

- [ ] **Step 2: Add a reusable master-key fixture to conftest**

Append to `tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    """Provide a valid Fernet master key for every test.

    Crypto/secret-store code fails closed without SECRET_ENC_KEY; set a
    fixed test key so unit tests can encrypt/decrypt deterministically.
    """
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SECRET_ENC_KEY", Fernet.generate_key().decode())
    yield
```

- [ ] **Step 3: Write the failing test**

Create `tests/test_crypto.py`:

```python
"""Unit tests for the Fernet master-key crypto core."""
import pytest

from core import crypto


def test_round_trip():
    token = crypto.encrypt(b"super secret")
    assert token != b"super secret"
    assert crypto.decrypt(token) == b"super secret"


def test_wrong_key_fails(monkeypatch):
    token = crypto.encrypt(b"data")
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SECRET_ENC_KEY", Fernet.generate_key().decode())
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(token)


def test_tampered_token_fails():
    token = bytearray(crypto.encrypt(b"data"))
    token[-1] ^= 0x01  # flip a bit
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(bytes(token))


def test_missing_key_is_fatal(monkeypatch):
    monkeypatch.delenv("SECRET_ENC_KEY", raising=False)
    with pytest.raises(crypto.MasterKeyError):
        crypto.validate_master_key()


def test_invalid_key_is_fatal(monkeypatch):
    monkeypatch.setenv("SECRET_ENC_KEY", "not-a-valid-fernet-key")
    with pytest.raises(crypto.MasterKeyError):
        crypto.validate_master_key()
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `pytest tests/test_crypto.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.crypto'`.

- [ ] **Step 5: Implement `core/crypto.py`**

Create `core/crypto.py`:

```python
"""Symmetric encryption for secrets at rest, keyed by an env-var master key.

The master key lives in SECRET_ENC_KEY (a urlsafe-base64 32-byte Fernet key).
The app fails closed if it is missing or malformed — better a clear startup
error than silently storing or returning garbage.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

_ENV_VAR = "SECRET_ENC_KEY"

_GENERATE_HINT = (
    'Generate one with: python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())" and set it as the '
    f"{_ENV_VAR} environment variable."
)


class MasterKeyError(RuntimeError):
    """SECRET_ENC_KEY is missing or not a valid Fernet key."""


class DecryptError(RuntimeError):
    """Ciphertext could not be decrypted (wrong key or tampered data)."""


def _load_fernet() -> Fernet:
    raw = (os.environ.get(_ENV_VAR) or "").strip()
    if not raw:
        raise MasterKeyError(f"{_ENV_VAR} is not set. {_GENERATE_HINT}")
    try:
        return Fernet(raw.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise MasterKeyError(
            f"{_ENV_VAR} is not a valid Fernet key. {_GENERATE_HINT}"
        ) from exc


def validate_master_key() -> None:
    """Raise MasterKeyError if the key is missing/invalid. Call at startup."""
    _load_fernet()


def encrypt(data: bytes) -> bytes:
    return _load_fernet().encrypt(data)


def decrypt(token: bytes) -> bytes:
    try:
        return _load_fernet().decrypt(token)
    except InvalidToken as exc:
        raise DecryptError("Could not decrypt secret (wrong key or tampered).") from exc
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `pytest tests/test_crypto.py -v`
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt core/crypto.py tests/test_crypto.py tests/conftest.py
git commit -m "feat(crypto): Fernet master-key encrypt/decrypt core"
```

---

## Task 2: `secrets` table + encrypted secret store

**Files:**
- Modify: `core/db.py` (`init_db`, after the `external_calendar_items` block ~line 130)
- Create: `core/secrets_store.py`
- Create: `tests/test_secrets_store.py`

- [ ] **Step 1: Add the `secrets` table to `init_db()`**

In `core/db.py`, inside `init_db()`, immediately after the `idx_ext_iso_date` index creation (around line 130, before the idempotent `upload_history` ALTER block), add:

```python
        conn.execute("""
            CREATE TABLE IF NOT EXISTS secrets (
                name TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                value BLOB NOT NULL,
                updated_at TEXT
            )
        """)
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_secrets_store.py`:

```python
"""Unit tests for the encrypted secret store."""
import os

import pytest

from core import secrets_store


@pytest.fixture(autouse=True)
def _db(temp_db):
    # temp_db (conftest) inits the schema on the isolated state.db.
    yield


def test_kv_round_trip():
    secrets_store.set_secret("api.key", "abc123")
    assert secrets_store.get_secret("api.key") == "abc123"


def test_get_unset_returns_none():
    assert secrets_store.get_secret("does.not.exist") is None


def test_has_secret():
    assert secrets_store.has_secret("x") is False
    secrets_store.set_secret("x", "y")
    assert secrets_store.has_secret("x") is True


def test_overwrite():
    secrets_store.set_secret("k", "v1")
    secrets_store.set_secret("k", "v2")
    assert secrets_store.get_secret("k") == "v2"


def test_delete():
    secrets_store.set_secret("k", "v")
    secrets_store.delete_secret("k")
    assert secrets_store.get_secret("k") is None


def test_list_names():
    secrets_store.set_secret("a", "1")
    secrets_store.set_secret("b", "2")
    assert set(secrets_store.list_secret_names()) == {"a", "b"}


def test_value_is_encrypted_at_rest():
    secrets_store.set_secret("k", "PLAINTEXT_MARKER")
    from core.db import _get_conn
    with _get_conn() as conn:
        row = conn.execute("SELECT value FROM secrets WHERE name='k'").fetchone()
    assert b"PLAINTEXT_MARKER" not in bytes(row["value"])


def test_blob_round_trip():
    secrets_store.set_blob("session", b"\x00\x01binarydata\xff")
    assert secrets_store.get_blob("session") == b"\x00\x01binarydata\xff"


def test_materialize_blob_to_tempfile_creates_then_removes():
    secrets_store.set_blob("file", b"contents")
    seen_path = None
    with secrets_store.materialize_blob_to_tempfile("file") as path:
        seen_path = path
        assert os.path.exists(path)
        with open(path, "rb") as f:
            assert f.read() == b"contents"
    assert not os.path.exists(seen_path)


def test_corrupt_secret_returns_none(monkeypatch):
    secrets_store.set_secret("k", "v")
    # Rotate the key so the stored ciphertext can't be decrypted.
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SECRET_ENC_KEY", Fernet.generate_key().decode())
    assert secrets_store.get_secret("k") is None
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/test_secrets_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.secrets_store'`.

- [ ] **Step 4: Implement `core/secrets_store.py`**

Create `core/secrets_store.py`:

```python
"""Encrypted secret store backed by the `secrets` table in state.db.

Values are Fernet-encrypted (core.crypto) before they touch disk. Two kinds:
  - 'kv'   : UTF-8 string secrets (API keys, password hashes)
  - 'blob' : arbitrary bytes (OAuth token JSON, Playwright storage_state)

`materialize_blob_to_tempfile` decrypts a blob to a 0600 temp file for the
brief window a third-party library needs a real file path, then deletes it.
"""
from __future__ import annotations

import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

from core import crypto
from core.db import _get_conn

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set(name: str, kind: str, raw: bytes) -> None:
    token = crypto.encrypt(raw)
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO secrets (name, kind, value, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, "
            "value=excluded.value, updated_at=excluded.updated_at",
            (name, kind, token, _now()),
        )


def _get_raw(name: str) -> bytes | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM secrets WHERE name=?", (name,)
        ).fetchone()
    if row is None:
        return None
    try:
        return crypto.decrypt(bytes(row["value"]))
    except crypto.DecryptError:
        # Spec: treat an undecryptable secret as unset, but log loudly so the
        # operator knows a key rotation or corruption happened.
        log.error("Secret %r could not be decrypted; treating as unset.", name)
        return None


def set_secret(name: str, plaintext: str) -> None:
    _set(name, "kv", plaintext.encode("utf-8"))


def get_secret(name: str) -> str | None:
    raw = _get_raw(name)
    return None if raw is None else raw.decode("utf-8")


def set_blob(name: str, data: bytes) -> None:
    _set(name, "blob", data)


def get_blob(name: str) -> bytes | None:
    return _get_raw(name)


def has_secret(name: str) -> bool:
    with _get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM secrets WHERE name=?", (name,)
        ).fetchone() is not None


def delete_secret(name: str) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM secrets WHERE name=?", (name,))


def list_secret_names() -> list[str]:
    with _get_conn() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM secrets").fetchall()]


@contextmanager
def materialize_blob_to_tempfile(name: str, suffix: str = ""):
    """Decrypt a blob secret to a 0600 temp file; delete it on exit.

    Yields the temp-file path, or None if the secret is unset.
    """
    data = get_blob(name)
    if data is None:
        yield None
        return
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/test_secrets_store.py -v`
Expected: 10 passed.

- [ ] **Step 6: Commit**

```bash
git add core/db.py core/secrets_store.py tests/test_secrets_store.py
git commit -m "feat(secrets): encrypted KV+blob store with temp-file materialization"
```

---

## Task 3: Shared-credential auth core

**Files:**
- Create: `core/auth.py`
- Create: `tests/test_auth_core.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth_core.py`:

```python
"""Unit tests for the shared-credential auth core."""
import pytest

from core import auth


@pytest.fixture(autouse=True)
def _db(temp_db):
    auth.reset_lockouts()
    yield


def test_not_configured_initially():
    assert auth.is_configured() is False


def test_set_and_verify_password():
    auth.set_password("hunter2")
    assert auth.is_configured() is True
    assert auth.verify_password("hunter2") is True
    assert auth.verify_password("wrong") is False


def test_hash_not_stored_plaintext():
    auth.set_password("plaintextpw")
    from core import secrets_store
    stored = secrets_store.get_secret(auth._HASH_SECRET)
    assert stored is not None
    assert "plaintextpw" not in stored


def test_bootstrap_from_env_seeds_password(monkeypatch):
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "seeded-pw")
    auth.bootstrap_from_env()
    assert auth.verify_password("seeded-pw") is True


def test_bootstrap_does_not_overwrite_existing(monkeypatch):
    auth.set_password("original")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "should-be-ignored")
    auth.bootstrap_from_env()
    assert auth.verify_password("original") is True


def test_lockout_after_threshold():
    auth.set_password("pw")
    ip = "10.0.0.5"
    for _ in range(auth.MAX_ATTEMPTS):
        assert auth.is_locked(ip) is False
        auth.record_failure(ip)
    assert auth.is_locked(ip) is True


def test_success_clears_failures():
    ip = "10.0.0.6"
    auth.record_failure(ip)
    auth.clear_failures(ip)
    assert auth.is_locked(ip) is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_auth_core.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.auth'`.

- [ ] **Step 3: Implement `core/auth.py`**

Create `core/auth.py`:

```python
"""Single shared-credential authentication.

The password hash (Werkzeug scrypt) is stored in the encrypted secret store
under `auth.password_hash`. First-boot bootstrap seeds it from the
INITIAL_ADMIN_PASSWORD env var so an open VPS has no public "set password"
page to race. A small in-process per-IP lockout slows brute force.
"""
from __future__ import annotations

import logging
import os
import time

from werkzeug.security import check_password_hash, generate_password_hash

from core import secrets_store

log = logging.getLogger(__name__)

_HASH_SECRET = "auth.password_hash"
_INITIAL_ENV = "INITIAL_ADMIN_PASSWORD"

MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300

# ip -> (failure_count, first_failure_monotonic)
_failures: dict[str, tuple[int, float]] = {}


def is_configured() -> bool:
    return secrets_store.has_secret(_HASH_SECRET)


def set_password(password: str) -> None:
    secrets_store.set_secret(_HASH_SECRET, generate_password_hash(password))


def verify_password(password: str) -> bool:
    stored = secrets_store.get_secret(_HASH_SECRET)
    if not stored:
        return False
    return check_password_hash(stored, password)


def bootstrap_from_env() -> None:
    """Seed the credential from INITIAL_ADMIN_PASSWORD if none is set yet."""
    if is_configured():
        return
    seed = (os.environ.get(_INITIAL_ENV) or "").strip()
    if not seed:
        log.warning(
            "No login credential is set and %s is not provided. Set %s and "
            "restart to create the initial login.", _INITIAL_ENV, _INITIAL_ENV,
        )
        return
    set_password(seed)
    log.info("Seeded initial login credential from %s.", _INITIAL_ENV)


def record_failure(ip: str) -> None:
    count, first = _failures.get(ip, (0, time.monotonic()))
    _failures[ip] = (count + 1, first)


def clear_failures(ip: str) -> None:
    _failures.pop(ip, None)


def is_locked(ip: str) -> bool:
    entry = _failures.get(ip)
    if entry is None:
        return False
    count, first = entry
    if time.monotonic() - first > LOCKOUT_SECONDS:
        _failures.pop(ip, None)  # window elapsed; reset
        return False
    return count >= MAX_ATTEMPTS


def reset_lockouts() -> None:
    """Test helper: clear all tracked failures."""
    _failures.clear()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_auth_core.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add core/auth.py tests/test_auth_core.py
git commit -m "feat(auth): shared-credential hash, env bootstrap, per-IP lockout"
```

---

## Task 4: Login/logout routes + `login_required`

**Files:**
- Create: `blueprints/auth.py`
- Create: `templates/login.html`
- Create: `tests/test_auth_routes.py`

- [ ] **Step 1: Implement the auth blueprint**

Create `blueprints/auth.py`:

```python
"""Login/logout routes and the login_required decorator."""
from __future__ import annotations

from functools import wraps

from flask import (
    Blueprint, redirect, render_template, request, session, url_for,
)

from core import auth

bp = Blueprint("auth", __name__)

_SESSION_KEY = "authenticated"


def is_authenticated() -> bool:
    return bool(session.get(_SESSION_KEY))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@bp.route("/login", methods=["GET"])
def login():
    if is_authenticated():
        return redirect(url_for("scan.index"))
    return render_template("login.html", error=None)


@bp.route("/login", methods=["POST"])
def login_submit():
    ip = request.remote_addr or "unknown"
    if auth.is_locked(ip):
        return render_template(
            "login.html",
            error="Too many failed attempts. Try again later.",
        ), 429
    password = request.form.get("password", "")
    if auth.verify_password(password):
        auth.clear_failures(ip)
        session[_SESSION_KEY] = True
        session.permanent = True
        nxt = request.args.get("next") or url_for("scan.index")
        # Only allow relative redirects (no open-redirect to other hosts).
        if not nxt.startswith("/"):
            nxt = url_for("scan.index")
        return redirect(nxt)
    auth.record_failure(ip)
    return render_template("login.html", error="Incorrect password."), 401


@bp.route("/logout", methods=["POST"])
def logout():
    session.pop(_SESSION_KEY, None)
    return redirect(url_for("auth.login"))
```

> Note: `scan.index` is the existing index route (`blueprints/scan.py`, `@bp.route("/")`). Confirm the blueprint name is `scan` when wiring; adjust `url_for` if different.

- [ ] **Step 2: Create the login template**

Create `templates/login.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sign in — Daily Life Distributor</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0;
           display: flex; min-height: 100vh; align-items: center; justify-content: center; margin: 0; }
    form { background: #1e293b; padding: 2rem; border-radius: 12px; width: 320px; }
    h1 { font-size: 1.1rem; margin: 0 0 1rem; }
    input { width: 100%; padding: .6rem; margin: .4rem 0 1rem; border-radius: 6px;
            border: 1px solid #334155; background: #0f172a; color: #e2e8f0; box-sizing: border-box; }
    button { width: 100%; padding: .6rem; border: 0; border-radius: 6px;
             background: #2563eb; color: white; font-weight: 600; cursor: pointer; }
    .error { color: #f87171; font-size: .85rem; margin-bottom: .5rem; }
  </style>
</head>
<body>
  <form method="POST" action="{{ url_for('auth.login_submit', next=request.args.get('next', '')) }}">
    <h1>Daily Life Distributor</h1>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <label for="password">Password</label>
    <input type="password" id="password" name="password" autofocus autocomplete="current-password">
    <button type="submit">Sign in</button>
  </form>
</body>
</html>
```

- [ ] **Step 3: Write the failing test**

Create `tests/test_auth_routes.py`:

```python
"""Login/logout flow tests using the Flask test client."""
import pytest

from core import auth


@pytest.fixture()
def client(temp_db, monkeypatch):
    auth.reset_lockouts()
    auth.set_password("correct-horse")
    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def test_login_page_accessible_without_session(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"password" in resp.data.lower()


def test_login_success_sets_session(client):
    resp = client.post("/login", data={"password": "correct-horse"})
    assert resp.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("authenticated") is True


def test_login_failure(client):
    resp = client.post("/login", data={"password": "nope"})
    assert resp.status_code == 401
    with client.session_transaction() as sess:
        assert sess.get("authenticated") is None


def test_logout_clears_session(client):
    client.post("/login", data={"password": "correct-horse"})
    client.post("/logout")
    with client.session_transaction() as sess:
        assert sess.get("authenticated") is None
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `pytest tests/test_auth_routes.py -v`
Expected: FAIL — the `auth` blueprint isn't registered yet (404 on `/login`). This is expected; Task 5 wires it in.

- [ ] **Step 5: Commit (red — wiring lands in Task 5)**

```bash
git add blueprints/auth.py templates/login.html tests/test_auth_routes.py
git commit -m "feat(auth): login/logout blueprint + template (not yet wired)"
```

---

## Task 5: Wire auth into `app.py` and replace the loopback guard

**Files:**
- Modify: `app.py` (`create_app`, lines 89-180)
- Test: `tests/test_auth_routes.py` (from Task 4) + new `tests/test_access_gate.py`

- [ ] **Step 1: Add cookie config + key validation + bootstrap + blueprint registration**

In `app.py` `create_app()`, replace the `FLASK_SECRET_KEY` block's end and the DB-init region so that, right after `app.secret_key = secret` (line 104), you add:

```python
    # Secrets at rest require a valid master key — fail closed at startup with
    # a clear message rather than erroring deep inside an upload later.
    from core import crypto
    crypto.validate_master_key()

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=(
            os.environ.get("SESSION_COOKIE_SECURE", "true").lower()
            in ("1", "true", "yes")
        ),
    )
```

Then, immediately after the `_db.init_db()` / `backfill_external_ids()` try-block (after line 122), add the credential bootstrap (the store schema must exist first):

```python
    from core import auth as _auth
    _auth.bootstrap_from_env()
```

- [ ] **Step 2: Replace the loopback guard with an auth gate**

In `app.py`, delete the entire `_restrict_to_loopback` function (lines 124-143). Replace it with:

```python
    from blueprints.auth import bp as auth_bp, is_authenticated
    app.register_blueprint(auth_bp)

    # Endpoints reachable without a session: the login routes, the health
    # probe, and static assets. Everything else requires authentication.
    _PUBLIC_ENDPOINTS = {"auth.login", "auth.login_submit", "_health", "static"}

    _ALLOWED_HOSTS = {
        h.strip().lower()
        for h in os.environ.get("ALLOWED_HOSTS", "").split(",")
        if h.strip()
    }

    @app.before_request
    def _require_auth():
        # DNS-rebind / host-spoofing defense for the hosted context: when
        # ALLOWED_HOSTS is configured, the Host header must match one of them.
        # Unset (local dev) = no host restriction.
        if _ALLOWED_HOSTS:
            host_no_port = (request.host or "").lower().split(":", 1)[0]
            if host_no_port not in _ALLOWED_HOSTS:
                abort(403)

        if request.endpoint in _PUBLIC_ENDPOINTS:
            return
        if is_authenticated():
            return
        # XHR/JSON callers get a 401; browser navigations get redirected.
        wants_json = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in request.headers.get("Accept", "")
        )
        if wants_json:
            abort(401)
        return redirect(url_for("auth.login", next=request.path))
```

Ensure `redirect` and `url_for` are imported at the top of `app.py` (add to the existing `from flask import ...` line if missing).

- [ ] **Step 3: Keep the CSRF check (no change needed)**

The existing `_csrf_same_origin` `before_request` (lines 145-175) already uses `request.host_url`, which reflects the real host, so it works unchanged on a VPS. Leave it as-is.

- [ ] **Step 4: Write the access-gate test**

Create `tests/test_access_gate.py`:

```python
"""The auth gate replaces the loopback guard: unauthenticated -> login."""
import pytest

from core import auth


@pytest.fixture()
def client(temp_db):
    auth.reset_lockouts()
    auth.set_password("pw")
    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def test_health_is_public(client):
    resp = client.get("/health")
    assert resp.status_code in (200, 503)


def test_unauthenticated_redirects_to_login(client):
    resp = client.get("/")
    assert resp.status_code in (301, 302)
    assert "/login" in resp.headers["Location"]


def test_authenticated_reaches_index(client):
    client.post("/login", data={"password": "pw"})
    resp = client.get("/")
    assert resp.status_code == 200


def test_unauthenticated_xhr_gets_401(client):
    resp = client.get("/", headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 401


def test_allowed_hosts_rejects_foreign_host(monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "uploader.example.com")
    auth.set_password("pw")
    import importlib
    import app as flask_app_module
    importlib.reload(flask_app_module)  # rebuild create_app with the env set
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        resp = c.get("/health", headers={"Host": "evil.example.com"})
        assert resp.status_code == 403
```

- [ ] **Step 5: Run both auth test files**

Run: `pytest tests/test_auth_routes.py tests/test_access_gate.py -v`
Expected: all pass (Task 4 tests now pass too, since the blueprint is registered).

> If `test_allowed_hosts_rejects_foreign_host` interferes with other tests via module reload, isolate it: run `pytest tests/test_access_gate.py -v` separately and confirm green.

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_access_gate.py
git commit -m "feat(auth): replace loopback guard with session auth gate + host allowlist"
```

---

## Task 6: Migrate API keys (Unsplash/Pexels) to the store

**Files:**
- Modify: `core/image_gatherer.py` (lines ~218, ~278)
- Create: `tests/test_secret_env_fallback.py`

- [ ] **Step 1: Add a resolver helper and switch the call sites**

In `core/image_gatherer.py`, add this helper near the top (after imports):

```python
def _resolve_key(name: str) -> str:
    """Prefer the encrypted store; fall back to env during migration."""
    from core import secrets_store
    return (secrets_store.get_secret(name) or os.environ.get(name, "") or "").strip()
```

Change line ~218 from:

```python
    key = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
```

to:

```python
    key = _resolve_key("UNSPLASH_ACCESS_KEY")
```

Change line ~278 from:

```python
    key = os.environ.get("PEXELS_API_KEY", "").strip()
```

to:

```python
    key = _resolve_key("PEXELS_API_KEY")
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_secret_env_fallback.py`:

```python
"""API-key resolution prefers the store, falls back to env."""
import pytest

from core import image_gatherer, secrets_store


@pytest.fixture(autouse=True)
def _db(temp_db):
    yield


def test_store_value_preferred(monkeypatch):
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "from-env")
    secrets_store.set_secret("UNSPLASH_ACCESS_KEY", "from-store")
    assert image_gatherer._resolve_key("UNSPLASH_ACCESS_KEY") == "from-store"


def test_env_fallback(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "env-only")
    assert image_gatherer._resolve_key("PEXELS_API_KEY") == "env-only"


def test_unset_returns_empty(monkeypatch):
    monkeypatch.delenv("UNSPLASH_ACCESS_KEY", raising=False)
    assert image_gatherer._resolve_key("UNSPLASH_ACCESS_KEY") == ""
```

- [ ] **Step 3: Run the test to verify it passes** (implementation already added in Step 1)

Run: `pytest tests/test_secret_env_fallback.py -v`
Expected: 3 passed.

- [ ] **Step 4: Commit**

```bash
git add core/image_gatherer.py tests/test_secret_env_fallback.py
git commit -m "feat(secrets): resolve Unsplash/Pexels keys from store with env fallback"
```

---

## Task 7: Migrate YouTube `client_secrets.json` + `token.json` to the store

**Files:**
- Modify: `uploaders/youtube_uploader.py` (lines ~40-43, `_resolve_secrets_path`, `_token_path`, `_atomic_write_text`, load/save sites ~127-232)
- Create: `tests/test_youtube_secret_blobs.py`

Secret names: `youtube.client_secrets` (blob), `youtube.token` (kv string — `creds.to_json()` is a JSON string).

- [ ] **Step 1: Add store-backed token accessors**

In `uploaders/youtube_uploader.py`, add near the other helpers (after `_resolve_secrets_path`, ~line 55):

```python
_YT_CLIENT_SECRETS_NAME = "youtube.client_secrets"
_YT_TOKEN_NAME = "youtube.token"


def _load_token_json() -> str | None:
    """Return the stored token JSON, or None if not yet authorized."""
    from core import secrets_store
    return secrets_store.get_secret(_YT_TOKEN_NAME)


def _save_token_json(data: str) -> None:
    from core import secrets_store
    secrets_store.set_secret(_YT_TOKEN_NAME, data)


def _clear_token() -> None:
    from core import secrets_store
    secrets_store.delete_secret(_YT_TOKEN_NAME)
```

- [ ] **Step 2: Use the stored token for load/refresh/save**

The current code calls `Credentials.from_authorized_user_file(token_path, SCOPES)` and `_atomic_write_text(token_path, creds.to_json())`. Replace file reads/writes with the store. Concretely, in the credentials-loading function (around lines 160-232), change:

- Loading: instead of reading the file, build creds from the stored JSON string:

```python
        token_json = _load_token_json()
        if token_json:
            import json
            from google.oauth2.credentials import Credentials as _Creds
            creds = _Creds.from_authorized_user_info(json.loads(token_json), SCOPES)
        else:
            creds = None
```

- Saving (every place that did `_atomic_write_text(token_path, creds.to_json())`):

```python
        _save_token_json(creds.to_json())
```

- Deleting a stale/corrupt token (places that did `os.remove(token_path)`):

```python
        _clear_token()
```

For `client_secrets.json`, the OAuth library needs a file path. In the function that calls `InstalledAppFlow.from_client_secrets_file(secrets_path, SCOPES)` (~line 229), materialize from the store when present, else fall back to the on-disk path:

```python
        from core import secrets_store
        with secrets_store.materialize_blob_to_tempfile(
            _YT_CLIENT_SECRETS_NAME, suffix=".json"
        ) as stored_path:
            secrets_path = stored_path or _resolve_secrets_path()
            flow = InstalledAppFlow.from_client_secrets_file(secrets_path, SCOPES)
            creds = flow.run_local_server(port=0)
        _save_token_json(creds.to_json())
```

- [ ] **Step 3: Write the test**

Create `tests/test_youtube_secret_blobs.py`:

```python
"""YouTube token round-trips through the encrypted store."""
import json

import pytest

from core import secrets_store
from uploaders import youtube_uploader as yt


@pytest.fixture(autouse=True)
def _db(temp_db):
    yield


def test_token_save_load_clear():
    assert yt._load_token_json() is None
    yt._save_token_json(json.dumps({"refresh_token": "abc"}))
    assert json.loads(yt._load_token_json())["refresh_token"] == "abc"
    yt._clear_token()
    assert yt._load_token_json() is None


def test_token_encrypted_at_rest():
    yt._save_token_json(json.dumps({"refresh_token": "SENSITIVE"}))
    from core.db import _get_conn
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM secrets WHERE name=?", (yt._YT_TOKEN_NAME,)
        ).fetchone()
    assert b"SENSITIVE" not in bytes(row["value"])


def test_client_secrets_materializes_to_file():
    secrets_store.set_blob(yt._YT_CLIENT_SECRETS_NAME, b'{"installed": {}}')
    with secrets_store.materialize_blob_to_tempfile(
        yt._YT_CLIENT_SECRETS_NAME, suffix=".json"
    ) as path:
        with open(path) as f:
            assert json.load(f) == {"installed": {}}
```

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_youtube_secret_blobs.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add uploaders/youtube_uploader.py tests/test_youtube_secret_blobs.py
git commit -m "feat(secrets): store YouTube client_secrets/token encrypted"
```

---

## Task 8: Migrate Playwright `storage_state` to the store

**Files:**
- Modify: `core/playwright_session.py` (`_new_context` ~line 270, save sites ~201/404, `SessionConfig`)
- Create: `tests/test_playwright_session_secret.py`

Secret name pattern: derive a blob name from the existing `session_file` basename, e.g. `playwright.<basename-without-ext>` so each service (`simplecast_session.json` → `playwright.simplecast_session`) is distinct.

- [ ] **Step 1: Add store-backed session helpers**

In `core/playwright_session.py`, add module-level helpers (after the imports / before `class PlaywrightSession`):

```python
def _session_secret_name(session_file: str) -> str:
    import os
    base = os.path.splitext(os.path.basename(session_file))[0]
    return f"playwright.{base}"


def _load_session_blob_to(session_file: str) -> bool:
    """Write the stored encrypted session to session_file. Returns True if found."""
    from core import secrets_store
    data = secrets_store.get_blob(_session_secret_name(session_file))
    if data is None:
        return False
    import os
    with open(session_file, "wb") as f:
        f.write(data)
    os.chmod(session_file, 0o600)
    return True


def _persist_session_blob(session_file: str) -> None:
    """Read session_file back into the encrypted store after a save."""
    import os
    if not os.path.exists(session_file):
        return
    from core import secrets_store
    with open(session_file, "rb") as f:
        secrets_store.set_blob(_session_secret_name(session_file), f.read())
```

- [ ] **Step 2: Load from store before context creation; persist after save**

In `_new_context` (~line 270), before the `if ... storage_state` logic that references `self.config.session_file`, ensure the file is materialized from the store:

```python
        # Hydrate the on-disk session file from the encrypted store (if any)
        # so Playwright's storage_state= path finds it.
        _load_session_blob_to(self.config.session_file)
```

After each `_atomic_save_storage_state(self.context, self.config.session_file)` call (lines ~201 and ~404), add:

```python
        _persist_session_blob(self.config.session_file)
```

- [ ] **Step 3: Write the test**

Create `tests/test_playwright_session_secret.py`:

```python
"""Playwright session blobs round-trip through the encrypted store."""
import os

import pytest

from core import playwright_session as ps
from core import secrets_store


@pytest.fixture(autouse=True)
def _db(temp_db):
    yield


def test_secret_name_per_service():
    assert ps._session_secret_name("/x/simplecast_session.json") == "playwright.simplecast_session"
    assert ps._session_secret_name("/x/rock_session.json") == "playwright.rock_session"


def test_persist_then_load(tmp_path):
    session_file = str(tmp_path / "rock_session.json")
    with open(session_file, "w") as f:
        f.write('{"cookies": []}')
    ps._persist_session_blob(session_file)

    os.remove(session_file)
    assert ps._load_session_blob_to(session_file) is True
    with open(session_file) as f:
        assert f.read() == '{"cookies": []}'


def test_load_missing_returns_false(tmp_path):
    session_file = str(tmp_path / "absent_session.json")
    assert ps._load_session_blob_to(session_file) is False


def test_session_encrypted_at_rest(tmp_path):
    session_file = str(tmp_path / "vista_social_session.json")
    with open(session_file, "w") as f:
        f.write("COOKIE_SECRET")
    ps._persist_session_blob(session_file)
    from core.db import _get_conn
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM secrets WHERE name=?",
            (ps._session_secret_name(session_file),),
        ).fetchone()
    assert b"COOKIE_SECRET" not in bytes(row["value"])
```

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_playwright_session_secret.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add core/playwright_session.py tests/test_playwright_session_secret.py
git commit -m "feat(secrets): store Playwright session_state encrypted per service"
```

---

## Task 9: One-time plaintext importer + boot auto-import

**Files:**
- Create: `scripts/migrate_secrets.py`
- Modify: `app.py` (after the auth bootstrap in Task 5)
- Create: `tests/test_migrate_secrets.py`

- [ ] **Step 1: Implement the importer**

Create `scripts/migrate_secrets.py`:

```python
"""Idempotently import existing plaintext secrets into the encrypted store.

Imports:
  - API keys from env: UNSPLASH_ACCESS_KEY, PEXELS_API_KEY
  - YouTube: client_secrets.json (blob), token.json (kv string)
  - Playwright: *_session.json (blob, one per service)

Safe to run repeatedly: a secret already present in the store is left alone.
Run manually with `python -m scripts.migrate_secrets`, or let it run
automatically on app boot.
"""
from __future__ import annotations

import logging
import os

from core import secrets_store
from core.config import PROJECT_ROOT

log = logging.getLogger(__name__)

_ENV_KEYS = ("UNSPLASH_ACCESS_KEY", "PEXELS_API_KEY")
_SESSION_FILES = (
    "simplecast_session.json",
    "vista_social_session.json",
    "rock_session.json",
)


def _import_kv_from_env(name: str) -> bool:
    if secrets_store.has_secret(name):
        return False
    val = (os.environ.get(name) or "").strip()
    if not val:
        return False
    secrets_store.set_secret(name, val)
    return True


def _import_blob_from_file(name: str, path: str) -> bool:
    if secrets_store.has_secret(name):
        return False
    if not os.path.exists(path):
        return False
    with open(path, "rb") as f:
        secrets_store.set_blob(name, f.read())
    return True


def _import_kv_from_file(name: str, path: str) -> bool:
    if secrets_store.has_secret(name):
        return False
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        secrets_store.set_secret(name, f.read())
    return True


def run() -> list[str]:
    """Import any missing plaintext secrets. Returns names imported."""
    imported: list[str] = []

    for key in _ENV_KEYS:
        if _import_kv_from_env(key):
            imported.append(key)

    if _import_blob_from_file(
        "youtube.client_secrets", os.path.join(PROJECT_ROOT, "client_secrets.json")
    ):
        imported.append("youtube.client_secrets")
    if _import_kv_from_file(
        "youtube.token", os.path.join(PROJECT_ROOT, "token.json")
    ):
        imported.append("youtube.token")

    for fname in _SESSION_FILES:
        base = os.path.splitext(fname)[0]
        if _import_blob_from_file(
            f"playwright.{base}", os.path.join(PROJECT_ROOT, fname)
        ):
            imported.append(f"playwright.{base}")

    if imported:
        log.info("Imported %d plaintext secret(s) into the store: %s",
                 len(imported), ", ".join(imported))
    return imported


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    names = run()
    print(f"Imported {len(names)} secret(s): {', '.join(names) or '(none)'}")
```

> Note: confirm `core.config.PROJECT_ROOT` exists (it's referenced in `core/env_validation.py:_check_youtube_secrets`). If not, use `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`.

- [ ] **Step 2: Auto-run on boot**

In `app.py`, immediately after `_auth.bootstrap_from_env()` (Task 5, Step 1), add:

```python
    try:
        from scripts.migrate_secrets import run as _migrate_secrets
        _migrate_secrets()
    except Exception:
        logging.getLogger(__name__).exception(
            "Secret auto-import failed; continuing (run python -m scripts.migrate_secrets manually)."
        )
```

- [ ] **Step 3: Write the test**

Create `tests/test_migrate_secrets.py`:

```python
"""The plaintext importer is correct and idempotent."""
import pytest

from core import secrets_store
from scripts import migrate_secrets


@pytest.fixture(autouse=True)
def _db(temp_db):
    yield


def test_imports_env_key(monkeypatch):
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "u-key")
    imported = migrate_secrets.run()
    assert "UNSPLASH_ACCESS_KEY" in imported
    assert secrets_store.get_secret("UNSPLASH_ACCESS_KEY") == "u-key"


def test_imports_token_file(monkeypatch, tmp_path):
    monkeypatch.setattr(migrate_secrets, "PROJECT_ROOT", str(tmp_path))
    (tmp_path / "token.json").write_text('{"refresh_token": "r"}')
    imported = migrate_secrets.run()
    assert "youtube.token" in imported
    assert secrets_store.get_secret("youtube.token") == '{"refresh_token": "r"}'


def test_idempotent(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "p-key")
    first = migrate_secrets.run()
    second = migrate_secrets.run()
    assert "PEXELS_API_KEY" in first
    assert "PEXELS_API_KEY" not in second  # already present, not re-imported


def test_does_not_overwrite_existing(monkeypatch):
    secrets_store.set_secret("UNSPLASH_ACCESS_KEY", "store-value")
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "env-value")
    migrate_secrets.run()
    assert secrets_store.get_secret("UNSPLASH_ACCESS_KEY") == "store-value"
```

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_migrate_secrets.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_secrets.py app.py tests/test_migrate_secrets.py
git commit -m "feat(secrets): idempotent plaintext importer + boot auto-import"
```

---

## Task 10: Settings UI — secrets panel + change password

**Files:**
- Modify: `blueprints/settings.py` (add routes)
- Modify: `templates/settings.html` (add panel)
- Create: `tests/test_settings_secrets.py`

- [ ] **Step 1: Add the routes**

In `blueprints/settings.py`, add (using the existing `bp` and the same patterns as the other POST routes):

```python
@bp.route("/settings/set-secret", methods=["POST"])
def set_secret_route():
    from flask import redirect, request, url_for
    from core import secrets_store
    name = (request.form.get("name") or "").strip()
    value = request.form.get("value") or ""
    if name and value:
        secrets_store.set_secret(name, value)
    return redirect(url_for("settings.settings"))


@bp.route("/settings/clear-secret", methods=["POST"])
def clear_secret_route():
    from flask import redirect, request, url_for
    from core import secrets_store
    name = (request.form.get("name") or "").strip()
    if name:
        secrets_store.delete_secret(name)
    return redirect(url_for("settings.settings"))


@bp.route("/settings/change-password", methods=["POST"])
def change_password_route():
    from flask import redirect, request, url_for
    from core import auth
    current = request.form.get("current") or ""
    new = request.form.get("new") or ""
    if new and auth.verify_password(current):
        auth.set_password(new)
    return redirect(url_for("settings.settings"))
```

> Confirm the settings index route's endpoint name (it's `@bp.route("/settings")` in `blueprints/settings.py`). Adjust the `url_for("settings.settings")` target to match the actual function name.

- [ ] **Step 2: Add the panel to the settings template**

In `templates/settings.html`, add a section (match existing markup/styling conventions in that file):

```html
<section class="settings-section">
  <h2>Secrets</h2>
  <p>Status only — stored values are never shown.</p>
  <table>
    <tbody>
      {% for name in secret_names %}
      <tr>
        <td>{{ name }}</td>
        <td>set</td>
        <td>
          <form method="POST" action="{{ url_for('settings.clear_secret_route') }}" style="display:inline">
            <input type="hidden" name="name" value="{{ name }}">
            <button type="submit">Clear</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <form method="POST" action="{{ url_for('settings.set_secret_route') }}">
    <input name="name" placeholder="Secret name (e.g. PEXELS_API_KEY)">
    <input name="value" type="password" placeholder="Value">
    <button type="submit">Save secret</button>
  </form>
</section>

<section class="settings-section">
  <h2>Change password</h2>
  <form method="POST" action="{{ url_for('settings.change_password_route') }}">
    <input name="current" type="password" placeholder="Current password" autocomplete="current-password">
    <input name="new" type="password" placeholder="New password" autocomplete="new-password">
    <button type="submit">Change password</button>
  </form>
</section>
```

- [ ] **Step 3: Pass `secret_names` to the template**

In the settings index view function in `blueprints/settings.py`, add `secret_names` to the `render_template(...)` context:

```python
    from core import secrets_store
    # ... existing context ...
    secret_names = [n for n in secrets_store.list_secret_names()
                    if n != "auth.password_hash"]
```

Add `secret_names=secret_names` to the `render_template` call's kwargs.

- [ ] **Step 4: Write the test**

Create `tests/test_settings_secrets.py`:

```python
"""Settings secret management + password change."""
import pytest

from core import auth, secrets_store


@pytest.fixture()
def client(temp_db):
    auth.reset_lockouts()
    auth.set_password("oldpw")
    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        c.post("/login", data={"password": "oldpw"})
        yield c


def test_set_and_clear_secret(client):
    client.post("/settings/set-secret", data={"name": "PEXELS_API_KEY", "value": "p"})
    assert secrets_store.get_secret("PEXELS_API_KEY") == "p"
    client.post("/settings/clear-secret", data={"name": "PEXELS_API_KEY"})
    assert secrets_store.get_secret("PEXELS_API_KEY") is None


def test_change_password_requires_current(client):
    client.post("/settings/change-password", data={"current": "wrong", "new": "newpw"})
    assert auth.verify_password("oldpw") is True  # unchanged
    client.post("/settings/change-password", data={"current": "oldpw", "new": "newpw"})
    assert auth.verify_password("newpw") is True
```

- [ ] **Step 5: Run the test**

Run: `pytest tests/test_settings_secrets.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add blueprints/settings.py templates/settings.html tests/test_settings_secrets.py
git commit -m "feat(settings): secrets status panel + change-password"
```

---

## Task 11: Documentation + full-suite verification

**Files:**
- Modify: `README.md` (env reference + access model)
- Modify: `CLAUDE.md` (auth/secret-store architecture note)

- [ ] **Step 1: Update the README env reference**

In `README.md`, add to the env var table:

```markdown
| `SECRET_ENC_KEY` | (required) | Fernet master key for the encrypted secret store. App refuses to start without it. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `INITIAL_ADMIN_PASSWORD` | (first boot) | Seeds the shared login credential on first start; change it later in Settings. |
| `ALLOWED_HOSTS` | (unset) | Comma-separated hostnames the app accepts (your VPS domain). Unset = no host restriction (local dev). |
| `SESSION_COOKIE_SECURE` | `true` | Set `false` for local http development. |
```

And update the Security model section to state that access is now gated by a shared login (not loopback), and secrets are encrypted at rest in `state.db`.

- [ ] **Step 2: Add a CLAUDE.md architecture note**

In `CLAUDE.md`, add a short section describing `core/crypto.py`, `core/secrets_store.py`, `core/auth.py`, the `secrets` table, and that all secrets are encrypted at rest and managed via Settings.

- [ ] **Step 3: Run the full test suite**

Run: `pytest -q` (integration tests under `tests/integration/` stay skipped by default)
Expected: all unit tests pass, including the new auth/secret suites.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document auth gate, secret store, and new env vars"
```

---

## Self-Review (completed during planning)

**Spec coverage:**
- §1 access control → Task 5. §2 auth → Tasks 3-5. §3 crypto → Task 1. §4 store → Task 2. §5 migrate KV → Task 6; YouTube → Task 7; Playwright → Task 8; importer → Task 9. §6 Settings UI → Task 10. §7 error handling → covered in Tasks 1 (fail-closed key), 2 (corrupt-secret→None), 3 (lockout) ; testing → each task's tests. Env/config table → Task 11.
- **Gap closed:** spec mentions documenting the new env vars — added as Task 11.

**Placeholders:** none — every code/test step contains complete code.

**Type/name consistency:** `_HASH_SECRET`, `_YT_TOKEN_NAME`, `_YT_CLIENT_SECRETS_NAME`, `_session_secret_name`, `materialize_blob_to_tempfile`, `is_authenticated`, `login_required` are defined once and referenced consistently. Secret-name conventions (`youtube.token`, `youtube.client_secrets`, `playwright.<service>`, `auth.password_hash`) match between the migration importer (Task 9) and the producing modules (Tasks 7-8).

**Assumptions verified against the codebase:** the index endpoint is `scan.index` (`blueprints/scan.py:25,29`); the settings index is `settings.settings` (`blueprints/settings.py:39,99`), so the `settings.set_secret_route` / `clear_secret_route` / `change_password_route` endpoints resolve under that blueprint; `core.config.PROJECT_ROOT` exists (`core/config.py:15`). No execution-time guesses remain.
```
