# Per-Org Credentials + Owner Impersonation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scope every credential the app stores to a specific organization, add a program-owner impersonation mechanism, and migrate existing legacy unscoped secrets into the bootstrap org's scope. Source spec: `docs/superpowers/specs/2026-05-24-per-org-credentials-design.md` (`ee90704`).

**Architecture:** New `core/org_context.py:effective_org_id()` overlays `session["acting_as_org_id"]` on top of the established `session["current_org_id"]`. Every production caller of `core.secrets_store` switches from unscoped lookups to `org_id=effective_org_id()`. `youtube.client_secrets` moves to a new platform-scoped namespace and becomes program-owner-only. Impersonation is a session flag with banner + audit; forbidden routes are blocked by a decorator. Agent path receives `org_id` in the `job_plan` envelope.

**Tech Stack:** Flask, SQLite (existing `core.db._get_conn`), Fernet (existing `core.crypto`), Playwright (existing uploaders), Jinja templates, pytest.

**Pre-conditions:** Branch off `main` at `ee90704` or later. CI must be green. Multi-tenant phases α–δ are live (no schema work for orgs/users/memberships/audit_log itself — only the new column).

---

## File Structure

**New files:**
- `core/org_context.py` — `effective_org_id()` / `is_impersonating()` / `acting_as_org_id()` + a `forbidden_during_impersonation` decorator
- `blueprints/impersonation.py` — `POST /admin/organizations/<id>/impersonate` + `POST /admin/exit-impersonation`
- `templates/_impersonation_banner.html` — partial included by `templates/base.html`
- `scripts/check_secret_scoping.py` — repo-level lint that fails CI when any production module under `core/`, `blueprints/`, `uploaders/` calls a `secrets_store` accessor without `org_id=` or the `platform_*` variant
- `tests/test_org_context.py`
- `tests/test_secret_platform_namespace.py`
- `tests/test_youtube_per_org_token.py`
- `tests/test_playwright_session_per_org.py`
- `tests/test_settings_admin_only_client_secrets.py`
- `tests/test_impersonation_flow.py`
- `tests/test_forbidden_during_impersonation.py`
- `tests/test_agent_dispatch_org_scope.py`
- `tests/test_legacy_secret_migration.py`
- `tests/test_check_secret_scoping.py`
- `tests/integration/test_cross_org_isolation.py`

**Modified files:**
- `core/db.py` — add `acting_as_org_id` column to `audit_log` + `audit_log_archive` (idempotent)
- `core/audit.py` — `write_event()` accepts + auto-fills `acting_as_org_id`
- `core/secrets_store.py` — add `set_platform_secret` / `get_platform_secret` / `set_platform_blob` / `get_platform_blob` / `has_platform_secret` / `delete_platform_secret`
- `uploaders/youtube_uploader.py` — token: per-org; client_secrets: platform
- `core/playwright_session.py` — every store accessor + every disk path becomes per-org
- `blueprints/settings.py` — env-style keys per-org; client_secrets row hidden + admin-only
- `core/image_gatherer.py` — `_resolve_key` per-org
- `core/agent_dispatch.py` — `collect_credentials(platforms_in_use, org_id)`; `start()` resolves org from session; envelope carries `org_id`
- `core/migration_bootstrap.py` — new step: rewrite legacy storage names into `org:<bootstrap>:<name>` and platform scope
- `blueprints/admin.py` — "Act as this org" button on the org detail page
- `app.py` — register `impersonation_bp`, context processor for the banner
- `templates/base.html` — include banner partial
- `.github/workflows/ci.yml` — add `python scripts/check_secret_scoping.py` step

---

## Phase 1: Org context + audit column

### Task 1.1: Add `acting_as_org_id` column to audit tables

**Files:**
- Modify: `core/db.py:273-295`
- Test: `tests/test_org_context.py` (new)

- [ ] **Step 1: Write the failing test**

`tests/test_org_context.py`:
```python
"""Schema: audit_log + audit_log_archive carry acting_as_org_id."""
from __future__ import annotations

from core import db as _db


def _columns(table: str) -> set[str]:
    with _db._get_conn() as c:
        return {r[1] for r in c.execute(f"PRAGMA table_info('{table}')").fetchall()}


def test_audit_log_has_acting_as_org_id():
    assert "acting_as_org_id" in _columns("audit_log")


def test_audit_log_archive_has_acting_as_org_id():
    assert "acting_as_org_id" in _columns("audit_log_archive")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_context.py -v`
Expected: 2 failures, both `KeyError`/AssertionError: column missing.

- [ ] **Step 3: Add the column to init_db()**

In `core/db.py` immediately after the existing `for _t in ("audit_log", "audit_log_archive"):` block (currently line 273-287), append:

```python
        # Phase per-org-creds: every audit row carries the impersonated org
        # (NULL when the actor was acting as themselves) so an investigator
        # can answer "what did the program owner do while acting as org N?"
        # in one query.
        for _t in ("audit_log", "audit_log_archive"):
            cols = {r[1] for r in conn.execute(
                f"PRAGMA table_info('{_t}')"
            ).fetchall()}
            if "acting_as_org_id" not in cols:
                conn.execute(
                    f"ALTER TABLE {_t} "
                    f"ADD COLUMN acting_as_org_id INTEGER"
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_context.py -v`
Expected: 2 passes.

- [ ] **Step 5: Commit**

```bash
git add core/db.py tests/test_org_context.py
git commit -m "feat(db): add acting_as_org_id column to audit_log + archive

Idempotent ALTER inside init_db(). Powers the per-org credential
impersonation audit trail — every action taken while acting as
another org gets recorded with both actor_user_id (the program
owner) and acting_as_org_id (the target).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 1.2: Build `core/org_context.py`

**Files:**
- Create: `core/org_context.py`
- Test: `tests/test_org_context.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_org_context.py`:
```python
import pytest
from flask import Flask

from core import org_context


@pytest.fixture()
def app_ctx():
    app = Flask(__name__)
    app.secret_key = "test"
    with app.test_request_context():
        yield


def test_effective_org_id_returns_none_outside_session(app_ctx):
    assert org_context.effective_org_id() is None


def test_effective_org_id_returns_current_when_not_acting(app_ctx):
    from flask import session
    session["current_org_id"] = 3
    assert org_context.effective_org_id() == 3
    assert org_context.is_impersonating() is False
    assert org_context.acting_as_org_id() is None


def test_effective_org_id_returns_acting_when_set(app_ctx):
    from flask import session
    session["current_org_id"] = 3
    session["acting_as_org_id"] = 11
    assert org_context.effective_org_id() == 11
    assert org_context.is_impersonating() is True
    assert org_context.acting_as_org_id() == 11


def test_real_user_id_is_always_session_user_id(app_ctx):
    from flask import session
    session["user_id"] = 7
    session["acting_as_org_id"] = 11
    assert org_context.real_user_id() == 7
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_org_context.py::test_effective_org_id_returns_none_outside_session -v`
Expected: ImportError — `core.org_context` does not exist.

- [ ] **Step 3: Write the module**

Create `core/org_context.py`:
```python
"""Effective-org resolver for credential reads + impersonation helpers.

Two session keys cooperate:

* ``current_org_id`` — the user's actively-selected membership org.
  Populated at login (auto for single-membership users, by the picker
  for multi-membership users). Permission decorators use this for role
  checks: a role check must always look at the *real* membership.

* ``acting_as_org_id`` — optional; set only by the program-owner
  impersonation route. While present, ``effective_org_id()`` returns
  this instead of ``current_org_id``. Real ``user_id`` never changes.

Credential reads MUST go through ``effective_org_id()``. Role checks
MUST keep reading ``session['current_org_id']`` directly so an owner
acting as another org cannot grant themselves a role they don't have.
"""
from __future__ import annotations

from functools import wraps
from typing import Optional

from flask import abort, session


def real_user_id() -> Optional[int]:
    """The authenticated user's id. Never affected by impersonation."""
    uid = session.get("user_id")
    return int(uid) if uid is not None else None


def current_org_id() -> Optional[int]:
    """The user's selected membership org. Real, not impersonated."""
    oid = session.get("current_org_id")
    return int(oid) if oid is not None else None


def acting_as_org_id() -> Optional[int]:
    """The org the program owner is impersonating, or None."""
    oid = session.get("acting_as_org_id")
    return int(oid) if oid is not None else None


def is_impersonating() -> bool:
    return acting_as_org_id() is not None


def effective_org_id() -> Optional[int]:
    """Org used for credential reads and audit org_id fill-ins.

    Returns acting_as_org_id when set, else current_org_id, else None.
    None means 'no session / no membership' — callers must treat that
    as a hard miss (don't fall back to legacy unscoped).
    """
    return acting_as_org_id() or current_org_id()


def forbidden_during_impersonation(view):
    """409 the request when ``acting_as_org_id`` is set.

    Applied to routes that change account security: 2FA setup/disable,
    password change, recovery approval, membership role changes. The
    program owner must exit impersonation before they're allowed to
    touch those endpoints on behalf of another tenant.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if is_impersonating():
            abort(409, description=(
                "This action is not allowed while acting as another "
                "organization. Exit impersonation and try again."
            ))
        return view(*args, **kwargs)
    return wrapped
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_org_context.py -v`
Expected: 6 passes (2 schema + 4 new).

- [ ] **Step 5: Commit**

```bash
git add core/org_context.py tests/test_org_context.py
git commit -m "feat(org-context): effective_org_id resolver + impersonation guard

New core/org_context.py is the single source of truth for
'which org owns the credentials this request needs?'. Session
keys: current_org_id (real membership) + acting_as_org_id
(program-owner impersonation overlay). Role decorators keep
using current_org_id directly so impersonation cannot escalate.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 1.3: Auto-fill `acting_as_org_id` in `audit.write_event`

**Files:**
- Modify: `core/audit.py`
- Modify: `core/db.py:823-834` (extend `insert_audit_event` signature)
- Test: `tests/test_audit_acting_as_org.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_audit_acting_as_org.py`:
```python
"""audit.write_event auto-fills acting_as_org_id from session."""
from __future__ import annotations

import pytest
from flask import Flask

from core import audit, db


@pytest.fixture()
def app_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    import importlib
    importlib.reload(db); db.init_db()
    app = Flask(__name__); app.secret_key = "test"
    with app.test_request_context():
        yield


def _last_event() -> dict:
    with db._get_conn() as c:
        row = c.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else {}


def test_write_event_records_acting_as_org_id_from_session(app_ctx):
    from flask import session
    session["acting_as_org_id"] = 42
    audit.write_event(action="test", actor_user_id=1, org_id=42)
    assert _last_event()["acting_as_org_id"] == 42


def test_write_event_records_null_when_not_impersonating(app_ctx):
    audit.write_event(action="test", actor_user_id=1, org_id=1)
    assert _last_event()["acting_as_org_id"] is None


def test_write_event_explicit_override_wins(app_ctx):
    from flask import session
    session["acting_as_org_id"] = 5
    audit.write_event(
        action="test", actor_user_id=1, org_id=5, acting_as_org_id=99,
    )
    assert _last_event()["acting_as_org_id"] == 99
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_audit_acting_as_org.py -v`
Expected: failures — `write_event` has no `acting_as_org_id` kwarg.

- [ ] **Step 3: Extend `insert_audit_event`**

In `core/db.py:823-834` change the function:
```python
def insert_audit_event(*, org_id, actor_user_id, action, target_type, target_id,
                       metadata, ip, user_agent, created_at,
                       acting_as_org_id=None) -> int:
    with _get_conn() as c:
        cur = c.execute(
            "INSERT INTO audit_log (org_id, actor_user_id, action, target_type, "
            "target_id, metadata, ip, user_agent, created_at, acting_as_org_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (org_id, actor_user_id, action, target_type, target_id,
             metadata, ip, user_agent, created_at, acting_as_org_id),
        )
        c.commit()
        return cur.lastrowid
```

- [ ] **Step 4: Extend `write_event`**

In `core/audit.py` replace `write_event` with:
```python
def write_event(
    *,
    action: str,
    actor_user_id: int | None = None,
    org_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    ip: str | None = None,
    ua: str | None = None,
    acting_as_org_id: int | None = None,
) -> int:
    """Persist an audit event and return its row id.

    When ``acting_as_org_id`` is not supplied, falls back to the
    flask session's ``acting_as_org_id`` value if a request context
    is active. Callers in non-request code paths can pass it
    explicitly (or leave it None for non-impersonated actions).
    """
    now = datetime.now(timezone.utc).isoformat()
    meta_json = json.dumps(metadata, default=str) if metadata is not None else None
    if acting_as_org_id is None:
        try:
            from flask import has_request_context, session
            if has_request_context():
                v = session.get("acting_as_org_id")
                if v is not None:
                    acting_as_org_id = int(v)
        except Exception:
            pass
    return _db.insert_audit_event(
        org_id=org_id,
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        metadata=meta_json,
        ip=ip,
        user_agent=ua,
        created_at=now,
        acting_as_org_id=acting_as_org_id,
    )
```

- [ ] **Step 5: Run all audit tests**

Run: `python -m pytest tests/test_audit_acting_as_org.py tests/test_audit_archive.py -v` (the archive test exists from earlier phases).
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add core/audit.py core/db.py tests/test_audit_acting_as_org.py
git commit -m "feat(audit): acting_as_org_id auto-filled from session

write_event() picks up session['acting_as_org_id'] when no
explicit value is passed, so every blueprint route that already
calls write_event() automatically records impersonation context.
Callers in non-request paths can still pass it explicitly.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Phase 2: Secret-store platform namespace

### Task 2.1: Add `platform:*` accessors to secrets_store

**Files:**
- Modify: `core/secrets_store.py`
- Test: `tests/test_secret_platform_namespace.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_secret_platform_namespace.py`:
```python
"""platform:<name> namespace for cross-tenant shared secrets."""
from __future__ import annotations

import pytest

from core import db, secrets_store


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    import importlib
    importlib.reload(db); db.init_db()
    yield


def test_set_then_get_platform_secret():
    secrets_store.set_platform_secret("foo", "v1")
    assert secrets_store.get_platform_secret("foo") == "v1"


def test_platform_and_org_share_a_name_but_not_storage():
    secrets_store.set_platform_secret("k", "platform-v")
    secrets_store.set_secret("k", "org-v", org_id=1)
    assert secrets_store.get_platform_secret("k") == "platform-v"
    assert secrets_store.get_secret("k", org_id=1) == "org-v"
    assert secrets_store.get_secret("k") is None  # legacy slot empty


def test_set_then_get_platform_blob():
    secrets_store.set_platform_blob("b", b"\\x00\\x01")
    assert secrets_store.get_platform_blob("b") == b"\\x00\\x01"


def test_has_and_delete_platform_secret():
    secrets_store.set_platform_secret("x", "y")
    assert secrets_store.has_platform_secret("x") is True
    secrets_store.delete_platform_secret("x")
    assert secrets_store.has_platform_secret("x") is False


def test_list_secret_names_excludes_platform_from_org_scope():
    secrets_store.set_platform_secret("p", "v")
    secrets_store.set_secret("o", "v", org_id=1)
    assert "p" not in secrets_store.list_secret_names(org_id=1)
    assert "p" not in secrets_store.list_secret_names()  # legacy listing
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_secret_platform_namespace.py -v`
Expected: all fail — `set_platform_secret` etc. don't exist.

- [ ] **Step 3: Add the platform wrappers**

In `core/secrets_store.py`, after the existing `delete_secret` (currently ~line 117), add:
```python
# ---------------------------------------------------------------------------
# Platform-scoped namespace
#
# Secrets that are shared across every tenant (the GCP OAuth client used by
# all orgs for YouTube authentication is the canonical example) live under
# the ``platform:<name>`` storage prefix. They are NOT visible from the
# per-org accessors above — reads MUST come through these wrappers, which
# guarantees no caller accidentally lands a tenant secret in platform scope
# (or vice versa).
# ---------------------------------------------------------------------------

_PLATFORM_PREFIX = "platform:"


def _platform_scoped(name: str) -> str:
    return f"{_PLATFORM_PREFIX}{name}"


def set_platform_secret(name: str, plaintext: str) -> None:
    _set(_platform_scoped(name), "kv", plaintext.encode("utf-8"), org_id=None)


def get_platform_secret(name: str) -> str | None:
    raw = _get_raw(_platform_scoped(name), org_id=None)
    return None if raw is None else raw.decode("utf-8")


def set_platform_blob(name: str, data: bytes) -> None:
    _set(_platform_scoped(name), "blob", data, org_id=None)


def get_platform_blob(name: str) -> bytes | None:
    return _get_raw(_platform_scoped(name), org_id=None)


def has_platform_secret(name: str) -> bool:
    with _get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM secrets WHERE name=?", (_platform_scoped(name),),
        ).fetchone() is not None


def delete_platform_secret(name: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM secrets WHERE name=?", (_platform_scoped(name),),
        )
        conn.commit()
```

Then update `list_secret_names` so legacy listing also strips `platform:` rows:
```python
def list_secret_names(*, org_id: int | None = None) -> list[str]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM secrets ORDER BY name"
        ).fetchall()
    if org_id is None:
        return [
            r["name"] for r in rows
            if not r["name"].startswith("org:")
            and not r["name"].startswith(_PLATFORM_PREFIX)
        ]
    prefix = f"org:{int(org_id)}:"
    return [
        r["name"][len(prefix):] for r in rows
        if r["name"].startswith(prefix)
    ]
```

Note: `_PLATFORM_PREFIX` must be defined above `list_secret_names` (or use the literal string). The example above assumes the new constant precedes the function. Move the function below the new block if needed.

Also `_scoped(name, org_id)` needs to refuse names that already start with `platform:` (defense in depth):
```python
def _scoped(name: str, org_id: int | None) -> str:
    if name.startswith("platform:") or name.startswith("org:"):
        raise ValueError(
            f"secret name {name!r} uses a reserved prefix; "
            "use set_platform_secret or pass org_id, not both."
        )
    if org_id is None:
        return name
    return f"org:{int(org_id)}:{name}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_secret_platform_namespace.py tests/test_secrets_store.py tests/test_org_scoped_secrets.py -v`
Expected: all pass (existing org-scoped tests must still pass).

- [ ] **Step 5: Commit**

```bash
git add core/secrets_store.py tests/test_secret_platform_namespace.py
git commit -m "feat(secrets): platform:<name> namespace for cross-tenant shared secrets

GCP OAuth client (youtube.client_secrets) and any future platform-
shared resources live here. Reads/writes go through dedicated
set_platform_*/get_platform_* wrappers so callers can't
accidentally cross the streams between tenant and platform scope.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2.2: Add `scripts/check_secret_scoping.py` lint

**Files:**
- Create: `scripts/check_secret_scoping.py`
- Create: `tests/test_check_secret_scoping.py`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Write the failing test**

Create `tests/test_check_secret_scoping.py`:
```python
"""Lint: production code must scope every secrets_store accessor."""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_lint_flags_unscoped_call(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text(textwrap.dedent('''
        from core import secrets_store
        def boom():
            secrets_store.get_secret("youtube.token")  # no org_id
    '''))
    res = subprocess.run(
        [sys.executable, "scripts/check_secret_scoping.py", str(f)],
        capture_output=True, text=True,
    )
    assert res.returncode == 1
    assert "secrets_store.get_secret" in res.stdout


def test_lint_passes_scoped_call(tmp_path):
    f = tmp_path / "good.py"
    f.write_text(textwrap.dedent('''
        from core import secrets_store
        from core.org_context import effective_org_id
        def ok():
            secrets_store.get_secret("youtube.token", org_id=effective_org_id())
    '''))
    res = subprocess.run(
        [sys.executable, "scripts/check_secret_scoping.py", str(f)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0


def test_lint_passes_platform_call(tmp_path):
    f = tmp_path / "good.py"
    f.write_text("from core import secrets_store\nsecrets_store.get_platform_secret('x')\n")
    res = subprocess.run(
        [sys.executable, "scripts/check_secret_scoping.py", str(f)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_check_secret_scoping.py -v`
Expected: all fail — script does not exist.

- [ ] **Step 3: Write the lint script**

Create `scripts/check_secret_scoping.py`:
```python
"""CI lint: refuse production secrets_store calls that don't scope themselves.

Production = core/, blueprints/, uploaders/ (the production import roots).
Allowed call shapes:
    secrets_store.get_secret(..., org_id=...)
    secrets_store.set_secret(..., org_id=...)
    secrets_store.set_platform_secret(...)
    secrets_store.get_platform_secret(...)
    ... and the blob/has/delete/list variants of each.

Disallowed:
    secrets_store.get_secret("foo")             # no org_id
    secrets_store.set_secret("foo", "bar")

The unscoped variants stay available for tests and migration tooling; this
lint is just the production-code gate.
"""
from __future__ import annotations

import ast
import pathlib
import sys

_SCOPED = {
    "get_secret", "set_secret", "delete_secret", "has_secret",
    "get_blob", "set_blob", "materialize_blob_to_tempfile",
    "list_secret_names",
}
_PLATFORM_ALLOWED = {
    "set_platform_secret", "get_platform_secret",
    "set_platform_blob", "get_platform_blob",
    "has_platform_secret", "delete_platform_secret",
}


def _is_secrets_store_call(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id == "secrets_store":
            return func.attr
    return None


def check_file(path: pathlib.Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    bad: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _is_secrets_store_call(node)
        if name is None:
            continue
        if name in _PLATFORM_ALLOWED:
            continue
        if name not in _SCOPED:
            continue
        has_org_kw = any(kw.arg == "org_id" for kw in node.keywords)
        if not has_org_kw:
            bad.append((node.lineno,
                        f"secrets_store.{name}() missing org_id="))
    return bad


_PROD_ROOTS = ("core/", "blueprints/", "uploaders/")
# Files we deliberately exempt: the secrets_store module itself, the
# migration script, and the agent shim which is a test double.
_EXEMPT = {
    "core/secrets_store.py",
    "core/migration_bootstrap.py",
    "scripts/migrate_secrets.py",
    "agent/secrets_shim.py",
}


def main(args: list[str]) -> int:
    if args:
        paths = [pathlib.Path(a) for a in args]
    else:
        repo = pathlib.Path(__file__).parent.parent
        paths = []
        for root in _PROD_ROOTS:
            paths.extend((repo / root).rglob("*.py"))
    failures: list[str] = []
    for p in paths:
        rel = p.as_posix()
        if any(rel.endswith(e) for e in _EXEMPT):
            continue
        for line, msg in check_file(p):
            failures.append(f"{rel}:{line}: {msg}")
    if failures:
        print("\\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run lint tests**

Run: `python -m pytest tests/test_check_secret_scoping.py -v`
Expected: 3 passes.

- [ ] **Step 5: Verify lint flags the existing repo**

This is the diagnostic baseline. Run:
```bash
python scripts/check_secret_scoping.py
```
Expected: many failures (every production call site listed in the spec). DO NOT FIX YET — the next phase fixes them. Capture the output for reference.

- [ ] **Step 6: Wire into CI (but allow failure for one phase)**

In `.github/workflows/ci.yml` add a new step BEFORE `Run tests`:
```yaml
      - name: Lint secret scoping
        # Allowed to fail until phase 3 lands the call-site plumbing.
        continue-on-error: true
        run: python scripts/check_secret_scoping.py
```

After Task 3.4 completes, **come back** and remove `continue-on-error: true`.

- [ ] **Step 7: Commit**

```bash
git add scripts/check_secret_scoping.py tests/test_check_secret_scoping.py .github/workflows/ci.yml
git commit -m "feat(ci): lint that refuses unscoped secrets_store calls

scripts/check_secret_scoping.py walks production code (core/,
blueprints/, uploaders/) and fails when it finds get_secret /
set_secret / get_blob / etc. without an explicit org_id= kwarg.
The platform_* variants are explicitly allowed. Wired into CI as
continue-on-error until phase 3 lands the call-site plumbing.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Phase 3: Plumb call sites

### Task 3.1: YouTube uploader — per-org token, platform client_secrets

**Files:**
- Modify: `uploaders/youtube_uploader.py:49-62, 289-303`
- Test: `tests/test_youtube_per_org_token.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_youtube_per_org_token.py`:
```python
"""YouTube uploader reads/writes the token per-org and the client_secrets per-platform."""
from __future__ import annotations

import json
import pytest
from flask import Flask

from core import db, secrets_store
from uploaders import youtube_uploader as yt


@pytest.fixture(autouse=True)
def _iso_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    import importlib
    importlib.reload(db); db.init_db()
    yield


@pytest.fixture()
def app_ctx():
    app = Flask(__name__); app.secret_key = "t"
    with app.test_request_context():
        yield


def test_load_token_reads_from_effective_org(app_ctx):
    from flask import session
    secrets_store.set_secret("youtube.token", '{"t":"a"}', org_id=1)
    secrets_store.set_secret("youtube.token", '{"t":"b"}', org_id=2)
    session["current_org_id"] = 1
    assert json.loads(yt._load_token_json())["t"] == "a"
    session["current_org_id"] = 2
    assert json.loads(yt._load_token_json())["t"] == "b"


def test_load_token_under_impersonation_reads_target(app_ctx):
    from flask import session
    secrets_store.set_secret("youtube.token", '{"t":"a"}', org_id=1)
    secrets_store.set_secret("youtube.token", '{"t":"b"}', org_id=2)
    session["current_org_id"] = 1
    session["acting_as_org_id"] = 2
    assert json.loads(yt._load_token_json())["t"] == "b"


def test_save_token_lands_in_effective_org(app_ctx):
    from flask import session
    session["current_org_id"] = 7
    yt._save_token_json('{"t":"new"}')
    assert secrets_store.get_secret("youtube.token", org_id=7) == '{"t":"new"}'
    # Legacy slot must not be written.
    assert secrets_store.get_secret("youtube.token") is None


def test_client_secrets_reads_platform_scope(app_ctx):
    secrets_store.set_platform_secret(
        "youtube.client_secrets",
        '{"web":{"client_id":"X"}}',
    )
    cfg = yt._load_client_config()
    assert cfg["web"]["client_id"] == "X"


def test_clear_token_removes_only_current_org(app_ctx):
    from flask import session
    secrets_store.set_secret("youtube.token", "a", org_id=1)
    secrets_store.set_secret("youtube.token", "b", org_id=2)
    session["current_org_id"] = 1
    yt._clear_token()
    assert secrets_store.get_secret("youtube.token", org_id=1) is None
    assert secrets_store.get_secret("youtube.token", org_id=2) == "b"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_youtube_per_org_token.py -v`
Expected: 5 failures — currently unscoped.

- [ ] **Step 3: Update the uploader**

In `uploaders/youtube_uploader.py` change lines 49–62 to:
```python
def _load_token_json() -> str | None:
    """Return the stored token JSON for the current org, or None."""
    from core import secrets_store
    from core.org_context import effective_org_id
    return secrets_store.get_secret(_YT_TOKEN_NAME, org_id=effective_org_id())


def _save_token_json(data: str) -> None:
    from core import secrets_store
    from core.org_context import effective_org_id
    secrets_store.set_secret(_YT_TOKEN_NAME, data, org_id=effective_org_id())


def _clear_token() -> None:
    from core import secrets_store
    from core.org_context import effective_org_id
    secrets_store.delete_secret(_YT_TOKEN_NAME, org_id=effective_org_id())
```

And in `_load_client_config` (line 289-303):
```python
def _load_client_config() -> dict:
    """Return the platform-shared OAuth client config dict.

    The GCP OAuth client is provisioned once by the program owner and
    used by every tenant for YouTube auth — only the resulting refresh
    token is per-org. Disk fallback (legacy single-tenant path) reads
    client_secrets.json from the project root.
    """
    from core import secrets_store
    raw = secrets_store.get_platform_secret(_YT_CLIENT_SECRETS_NAME)
    if raw:
        return json.loads(raw)
    path = _resolve_secrets_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Client secrets file not found: {path}. Program owner: "
            "upload it via Settings (admin-only)."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
```

- [ ] **Step 4: Run all YouTube tests**

Run: `python -m pytest tests/test_youtube_per_org_token.py tests/test_youtube_client_secrets_upload.py tests/test_youtube_secret_blobs.py -v`
Expected: new file passes; older two may break — they likely call unscoped accessors. If they fail with "missing org_id" or wrong scope, update them to set up a request context and set `session["current_org_id"]` before exercising the uploader. Do not skip them.

- [ ] **Step 5: Commit**

```bash
git add uploaders/youtube_uploader.py tests/test_youtube_per_org_token.py tests/test_youtube_client_secrets_upload.py tests/test_youtube_secret_blobs.py
git commit -m "feat(youtube): per-org token, platform-shared client_secrets

The OAuth refresh token is the per-tenant piece — every org auths
through the program owner's single GCP project, so client_secrets
stays platform-scoped. Token reads/writes now route through
effective_org_id() (acting-as-org overlay aware).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3.2: Playwright session helpers — per-org

**Files:**
- Modify: `core/playwright_session.py:65-139`
- Modify: `app.py` (drop the boot-time `materialize_known_sessions` call — we'll re-add a per-org version later if needed; today the on-demand `_load_session_blob_to` covers it)
- Test: `tests/test_playwright_session_per_org.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_playwright_session_per_org.py`:
```python
"""playwright_session blob helpers are per-org and write to per-org paths."""
from __future__ import annotations

import os
import pytest

from core import db, secrets_store, playwright_session as pws


@pytest.fixture(autouse=True)
def _iso_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    import importlib
    importlib.reload(db); db.init_db()
    yield tmp_path


def test_load_session_blob_routes_by_org(tmp_path):
    base = "simplecast_session.json"
    secrets_store.set_blob("playwright.simplecast_session", b'{"x":1}', org_id=1)
    secrets_store.set_blob("playwright.simplecast_session", b'{"x":2}', org_id=2)
    dst1 = tmp_path / "org1" / base; dst1.parent.mkdir()
    dst2 = tmp_path / "org2" / base; dst2.parent.mkdir()
    assert pws._load_session_blob_to(str(dst1), org_id=1)
    assert pws._load_session_blob_to(str(dst2), org_id=2)
    assert dst1.read_bytes() == b'{"x":1}'
    assert dst2.read_bytes() == b'{"x":2}'


def test_persist_session_blob_writes_to_target_org(tmp_path):
    f = tmp_path / "simplecast_session.json"
    f.write_bytes(b'{"y":42}')
    pws._persist_session_blob(str(f), org_id=5)
    assert secrets_store.get_blob("playwright.simplecast_session", org_id=5) == b'{"y":42}'
    assert secrets_store.get_blob("playwright.simplecast_session", org_id=6) is None


def test_has_session_checks_target_org(tmp_path):
    f = tmp_path / "simplecast_session.json"
    assert pws.has_session(str(f), org_id=1) is False
    secrets_store.set_blob("playwright.simplecast_session", b"x", org_id=1)
    assert pws.has_session(str(f), org_id=1) is True
    assert pws.has_session(str(f), org_id=2) is False


def test_clear_session_only_removes_target_org(tmp_path):
    f = tmp_path / "simplecast_session.json"
    secrets_store.set_blob("playwright.simplecast_session", b"a", org_id=1)
    secrets_store.set_blob("playwright.simplecast_session", b"b", org_id=2)
    pws.clear_session(str(f), org_id=1)
    assert secrets_store.get_blob("playwright.simplecast_session", org_id=1) is None
    assert secrets_store.get_blob("playwright.simplecast_session", org_id=2) == b"b"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_playwright_session_per_org.py -v`
Expected: failures — helpers take no `org_id`.

- [ ] **Step 3: Update the helpers**

In `core/playwright_session.py`:

Replace `_load_session_blob_to` (line 71–81):
```python
def _load_session_blob_to(session_file: str, *, org_id: int | None = None) -> bool:
    """Write the stored encrypted session to session_file.

    *org_id* selects which tenant's blob to materialize. None falls
    back to the legacy unscoped slot (used only by the migration
    bootstrap; production callers must pass it).
    """
    from core import secrets_store
    data = secrets_store.get_blob(_session_secret_name(session_file), org_id=org_id)
    if data is None:
        return False
    with open(session_file, "wb") as f:
        f.write(data)
    if os.name != "nt":
        os.chmod(session_file, 0o600)
    return True
```

Replace `_persist_session_blob` (line 84–90):
```python
def _persist_session_blob(session_file: str, *, org_id: int | None = None) -> None:
    """Read session_file back into the encrypted store under *org_id*."""
    if not os.path.exists(session_file):
        return
    from core import secrets_store
    with open(session_file, "rb") as f:
        secrets_store.set_blob(
            _session_secret_name(session_file), f.read(), org_id=org_id,
        )
```

Replace `has_session` (line 123–126):
```python
def has_session(session_file: str, *, org_id: int | None = None) -> bool:
    """True if a saved session exists in the store for *org_id* or on disk."""
    from core import secrets_store
    return (
        secrets_store.has_secret(_session_secret_name(session_file), org_id=org_id)
        or os.path.exists(session_file)
    )
```

Replace `clear_session` (line 129–139):
```python
def clear_session(session_file: str, *, org_id: int | None = None) -> None:
    """Clear a saved session (disk first, then store) at *org_id* scope."""
    if os.path.exists(session_file):
        os.remove(session_file)
    from core import secrets_store
    secrets_store.delete_secret(_session_secret_name(session_file), org_id=org_id)
```

Inside `PlaywrightSession._new_context` (line 370–393), look up `current_org` once and pass it:
```python
    def _new_context(self, *, with_session: bool) -> "BrowserContext":
        assert self.browser is not None
        from core.org_context import effective_org_id
        org_id = effective_org_id()
        ctx_kwargs: dict = {}
        if with_session:
            _load_session_blob_to(self.config.session_file, org_id=org_id)
        if with_session and os.path.isfile(self.config.session_file):
            ctx_kwargs["storage_state"] = self.config.session_file
        if self.config.viewport is not None:
            ctx_kwargs["viewport"] = self.config.viewport
        try:
            return self.browser.new_context(**ctx_kwargs)
        except Exception as e:
            if "storage_state" in ctx_kwargs:
                log.warning(
                    "%s: failed to load session file %s (%s) — retrying without it; "
                    "user will need to log in again.",
                    self.config.name, self.config.session_file, e,
                )
                ctx_kwargs.pop("storage_state", None)
                return self.browser.new_context(**ctx_kwargs)
            raise
```

Similarly update `_handle_login` and `__exit__` to call `_persist_session_blob(self.config.session_file, org_id=effective_org_id())`.

Also update `has_session` callers in `_open()` (line 418): `have_session = has_session(self.config.session_file, org_id=effective_org_id())`.

- [ ] **Step 4: Replace per-org disk path in PlaywrightSession**

Two orgs both running SimpleCast at once would clobber each other's `simplecast_session.json` at the project root. Override the path inside `_open()`:

```python
    def _open(self) -> None:
        """Land on a logged-in page, prompting the user if needed."""
        self._emit(PHASE_LAUNCHING)

        from core.org_context import effective_org_id
        org_id = effective_org_id()

        # Per-org disk path so two orgs running the same platform
        # in parallel don't clobber each other's session file.
        if org_id is not None:
            base = os.path.basename(self.config.session_file)
            parent = os.path.dirname(self.config.session_file)
            per_org_dir = os.path.join(parent, ".sessions", f"org_{org_id}")
            os.makedirs(per_org_dir, exist_ok=True)
            self.config.session_file = os.path.join(per_org_dir, base)

        have_session = has_session(self.config.session_file, org_id=org_id)
        # ... rest unchanged
```

- [ ] **Step 5: Drop `materialize_known_sessions` from boot**

In `app.py`, find the call to `materialize_known_sessions()` (search for the name) and remove it; per-org materialization is on-demand inside `_open()`. Leave the function in place for now (used by migration bootstrap later) but no longer invoked at startup.

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_playwright_session_per_org.py tests/test_remote_login.py -v`
Expected: new file passes. `test_remote_login.py` may have failures from the API change — add an `org_id=N` kwarg to its calls or wrap them in a request context with `session["current_org_id"]=N`.

- [ ] **Step 7: Commit**

```bash
git add core/playwright_session.py app.py tests/test_playwright_session_per_org.py tests/test_remote_login.py
git commit -m "feat(playwright): per-org session blobs + per-org disk paths

_load/_persist/_has/_clear_session_blob all take org_id. The
on-disk session file path is now .sessions/org_<id>/<basename>
inside the project root so two orgs running the same platform
concurrently don't clobber each other. Boot-time
materialize_known_sessions removed; sessions materialize on demand
from the org scope each PlaywrightSession opens.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3.3: settings.py — per-org keys, admin-only client_secrets

**Files:**
- Modify: `blueprints/settings.py:180-219, 599-627`
- Modify: `templates/settings.html` (conditional client_secrets row)
- Test: `tests/test_settings_admin_only_client_secrets.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_settings_admin_only_client_secrets.py`:
```python
"""client_secrets upload is admin-only; per-org users don't see the row."""
from __future__ import annotations

import io
import json
import pytest
from flask import Flask

from core import db, user_store, org_store
from core import secrets_store


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SECRET_ENC_KEY", "QcOzn6q5y8yp7v4OoH5sNcWZS_VqIyqU0o8jOwHjW6w=")
    monkeypatch.setenv("FLASK_SECRET_KEY", "t")
    import importlib
    import core.db, core.org_store, core.user_store, core.secrets_store
    importlib.reload(core.db); importlib.reload(core.org_store)
    importlib.reload(core.user_store); importlib.reload(core.secrets_store)
    core.db.init_db()
    import app as m; importlib.reload(m)
    return m.app


def _login_as(client, user_id, org_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["current_org_id"] = org_id
        sess["permission_2fa_passed"] = True


def test_org_owner_does_not_see_client_secrets_row(app):
    org = org_store.create_org(name="A", slug="a")
    owner = user_store.create_user(
        username="alice", email="a@x", password="pw1234567", program_owner=False,
    )
    org_store.add_membership(user_id=owner["id"], org_id=org["id"], role="owner")
    client = app.test_client()
    _login_as(client, owner["id"], org["id"])
    res = client.get("/settings")
    assert res.status_code == 200
    assert b"client_secrets.json" not in res.data


def test_org_owner_post_to_client_secrets_returns_403(app):
    org = org_store.create_org(name="A", slug="a")
    owner = user_store.create_user(
        username="alice", email="a@x", password="pw1234567", program_owner=False,
    )
    org_store.add_membership(user_id=owner["id"], org_id=org["id"], role="owner")
    client = app.test_client()
    _login_as(client, owner["id"], org["id"])
    res = client.post("/settings", data={
        "youtube_client_secrets": (io.BytesIO(json.dumps(
            {"web": {"client_id": "x"}}).encode()), "client_secrets.json"),
    }, content_type="multipart/form-data")
    assert res.status_code in (403, 302)
    assert secrets_store.has_platform_secret("youtube.client_secrets") is False


def test_program_owner_upload_lands_in_platform_scope(app):
    org = org_store.create_org(name="LCBC", slug="lcbc")
    po = user_store.create_user(
        username="po", email="po@x", password="pw1234567", program_owner=True,
    )
    org_store.add_membership(user_id=po["id"], org_id=org["id"], role="owner")
    client = app.test_client()
    _login_as(client, po["id"], org["id"])
    res = client.post("/settings", data={
        "youtube_client_secrets": (io.BytesIO(json.dumps(
            {"web": {"client_id": "x"}}).encode()), "client_secrets.json"),
    }, content_type="multipart/form-data", follow_redirects=False)
    assert res.status_code in (200, 302)
    assert secrets_store.has_platform_secret("youtube.client_secrets") is True
    assert secrets_store.has_secret("youtube.client_secrets") is False
    assert secrets_store.has_secret("youtube.client_secrets", org_id=org["id"]) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_settings_admin_only_client_secrets.py -v`
Expected: failures.

- [ ] **Step 3: Update settings POST handler**

In `blueprints/settings.py:180-194`, gate the `youtube_client_secrets` upload behind program-owner. Around the existing upload block:
```python
        # client_secrets is platform-shared (every tenant auths through the
        # program owner's GCP project). Only program-owners can upload it;
        # org users don't see the row, but a hand-crafted POST also fails here.
        from core import user_store
        from core.org_context import real_user_id
        uid = real_user_id()
        po = user_store.get_user_by_id(uid) if uid is not None else None
        is_program_owner = bool(po and po.get("program_owner"))
        client_secrets_file = request.files.get("youtube_client_secrets")
        if client_secrets_file and client_secrets_file.filename:
            if not is_program_owner:
                abort(403)
            blob = client_secrets_file.read()
            try:
                parsed = json.loads(blob)
            except json.JSONDecodeError:
                flash("client_secrets.json is not valid JSON.", "danger")
                return redirect(url_for("settings.settings"))
            if "installed" not in parsed and "web" not in parsed:
                flash("client_secrets.json missing 'installed' or 'web' key — "
                      "is this an OAuth client secrets file?", "danger")
                return redirect(url_for("settings.settings"))
            # Keep the on-disk copy for the legacy single-tenant USB path
            # (LEGACY_PASSWORD_ENABLED). Multi-tenant production reads from the
            # platform store.
            dest = os.path.join(PROJECT_ROOT, "client_secrets.json")
            try:
                with open(dest, "wb") as f:
                    f.write(blob)
            except OSError as e:
                flash(f"Could not save client_secrets.json ({e}).", "danger")
                return redirect(url_for("settings.settings"))
            from core import secrets_store
            secrets_store.set_platform_blob("youtube.client_secrets", blob)
```

- [ ] **Step 4: Update settings GET to compute `is_program_owner` and pass to template**

In `blueprints/settings.py:199-242`, compute the flag and feed the template; also switch the env-secret summary to per-org:
```python
    config = load_config()
    from core import user_store, secrets_store as _ss
    from core.org_context import real_user_id, effective_org_id
    uid = real_user_id()
    po = user_store.get_user_by_id(uid) if uid is not None else None
    is_program_owner = bool(po and po.get("program_owner"))

    secrets_path = os.path.join(PROJECT_ROOT, "client_secrets.json")
    client_secrets_found = (
        os.path.isfile(secrets_path) or _ss.has_platform_secret("youtube.client_secrets")
    )
    from core.playwright_session import has_session
    org_id = effective_org_id()
    simplecast_session_found = has_session(
        os.path.join(PROJECT_ROOT, "simplecast_session.json"), org_id=org_id,
    )
    vista_social_session_found = has_session(
        os.path.join(PROJECT_ROOT, "vista_social_session.json"), org_id=org_id,
    )
    rock_session_found = has_session(
        os.path.join(PROJECT_ROOT, "rock_session.json"), org_id=org_id,
    )

    from core.auth import _HASH_SECRET
    known_secrets = [
        {**spec, "is_set": _ss.has_secret(spec["name"], org_id=org_id)}
        for spec in KNOWN_SECRETS
    ]
    known_names = {spec["name"] for spec in KNOWN_SECRETS}
    extra_secrets = [n for n in _ss.list_secret_names(org_id=org_id)
                     if n != _HASH_SECRET and n not in known_names]
    return render_template(
        "settings.html",
        is_program_owner=is_program_owner,
        config=config,
        client_secrets_found=client_secrets_found,
        # ... existing kwargs ...
    )
```

- [ ] **Step 5: Wrap the client_secrets row in `templates/settings.html`**

Find the upload form's `<input type="file" name="youtube_client_secrets">` and wrap the surrounding block:
```jinja
{% if is_program_owner %}
  <!-- existing client_secrets upload row -->
{% endif %}
```

- [ ] **Step 6: Update `/settings/set-secret` and `/settings/clear-secret` to per-org**

In `blueprints/settings.py:599-627`:
```python
@bp.route("/settings/set-secret", methods=["POST"])
def set_secret_route():
    name = (request.form.get("name") or "").strip()
    value = request.form.get("value") or ""
    if not (name and value):
        flash("Secret name and value are both required.", "warning")
        return redirect(url_for("settings.settings"))
    try:
        from core import secrets_store
        from core.org_context import effective_org_id
        secrets_store.set_secret(name, value, org_id=effective_org_id())
        flash(f"Secret '{name}' saved.", "success")
    except Exception as e:
        flash(f"Could not save secret: {e}", "danger")
    return redirect(url_for("settings.settings"))


@bp.route("/settings/clear-secret", methods=["POST"])
def clear_secret_route():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("No secret specified.", "warning")
        return redirect(url_for("settings.settings"))
    try:
        from core import secrets_store
        from core.org_context import effective_org_id
        secrets_store.delete_secret(name, org_id=effective_org_id())
        flash(f"Secret '{name}' cleared.", "success")
    except Exception as e:
        flash(f"Could not clear secret: {e}", "danger")
    return redirect(url_for("settings.settings"))
```

- [ ] **Step 7: Run tests**

Run: `python -m pytest tests/test_settings_admin_only_client_secrets.py tests/test_settings_secrets.py -v`
Expected: new file passes; existing `test_settings_secrets.py` may need request-context fixes for the per-org change — fix them (set `current_org_id` in test sessions).

- [ ] **Step 8: Commit**

```bash
git add blueprints/settings.py templates/settings.html tests/test_settings_admin_only_client_secrets.py tests/test_settings_secrets.py
git commit -m "feat(settings): per-org env keys; client_secrets admin-only platform

Org users see only their org's API keys + Playwright session
status. The YouTube client_secrets upload row is hidden from
non-owners and the POST handler 403s if a non-owner tries to
submit it. Successful uploads land in platform: scope, not
in any tenant scope.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3.4: image_gatherer `_resolve_key` per-org + flip lint to required

**Files:**
- Modify: `core/image_gatherer.py:67-71`
- Modify: `.github/workflows/ci.yml` (remove `continue-on-error`)

- [ ] **Step 1: Update `_resolve_key`**

In `core/image_gatherer.py:67-71`:
```python
def _resolve_key(name: str) -> str:
    """Get API key from per-org secrets store, falling back to env."""
    from core import secrets_store
    from core.org_context import effective_org_id
    return (
        secrets_store.get_secret(name, org_id=effective_org_id())
        or os.environ.get(name, "")
        or ""
    ).strip()
```

- [ ] **Step 2: Flip CI lint to required**

In `.github/workflows/ci.yml`, remove the `continue-on-error: true` line on the lint step.

- [ ] **Step 3: Run the lint locally**

Run: `python scripts/check_secret_scoping.py`
Expected: exit 0 (no findings). If any remain, fix them — they're in production code and the lint refuses to let them through.

- [ ] **Step 4: Run the full test suite**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add core/image_gatherer.py .github/workflows/ci.yml
git commit -m "feat(image-gatherer): per-org API key resolve + lint required

Unsplash/Pexels keys now read from the current org's scope, with
env fallback intact. Flips the CI lint from advisory to required:
any future unscoped secrets_store call in production fails the
build.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Phase 4: Impersonation UI + guards

### Task 4.1: Impersonation blueprint + routes

**Files:**
- Create: `blueprints/impersonation.py`
- Modify: `app.py` (register the blueprint)
- Test: `tests/test_impersonation_flow.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_impersonation_flow.py`:
```python
"""Program-owner impersonation: enter, exit, audit, role-gate."""
from __future__ import annotations

import pytest

from core import db, user_store, org_store


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SECRET_ENC_KEY", "QcOzn6q5y8yp7v4OoH5sNcWZS_VqIyqU0o8jOwHjW6w=")
    monkeypatch.setenv("FLASK_SECRET_KEY", "t")
    import importlib
    import core.db, core.org_store, core.user_store, core.secrets_store
    importlib.reload(core.db); importlib.reload(core.org_store)
    importlib.reload(core.user_store); importlib.reload(core.secrets_store)
    core.db.init_db()
    import app as m; importlib.reload(m)
    return m.app


def _po(app):
    po_org = org_store.create_org(name="Bootstrap", slug="boot")
    po = user_store.create_user(
        username="po", email="po@x", password="pw1234567", program_owner=True,
    )
    org_store.add_membership(user_id=po["id"], org_id=po_org["id"], role="owner")
    target = org_store.create_org(name="Target", slug="target")
    return po, po_org, target


def _login(client, uid, org_id):
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["current_org_id"] = org_id
        sess["permission_2fa_passed"] = True


def test_owner_can_enter_and_exit_impersonation(app):
    po, po_org, target = _po(app)
    client = app.test_client()
    _login(client, po["id"], po_org["id"])
    res = client.post(f"/admin/organizations/{target['id']}/impersonate",
                      follow_redirects=False)
    assert res.status_code == 302
    with client.session_transaction() as s:
        assert s.get("acting_as_org_id") == target["id"]
    client.post("/admin/exit-impersonation", follow_redirects=False)
    with client.session_transaction() as s:
        assert s.get("acting_as_org_id") is None


def test_non_owner_cannot_enter_impersonation(app):
    po, po_org, target = _po(app)
    user = user_store.create_user(
        username="u", email="u@x", password="pw1234567", program_owner=False,
    )
    org_store.add_membership(user_id=user["id"], org_id=po_org["id"], role="owner")
    client = app.test_client()
    _login(client, user["id"], po_org["id"])
    res = client.post(f"/admin/organizations/{target['id']}/impersonate")
    assert res.status_code == 403


def test_impersonation_writes_audit_events(app):
    po, po_org, target = _po(app)
    client = app.test_client()
    _login(client, po["id"], po_org["id"])
    client.post(f"/admin/organizations/{target['id']}/impersonate")
    client.post("/admin/exit-impersonation")
    with db._get_conn() as c:
        rows = c.execute(
            "SELECT action, actor_user_id, acting_as_org_id "
            "FROM audit_log ORDER BY id"
        ).fetchall()
    actions = [r["action"] for r in rows]
    assert "impersonation.start" in actions
    assert "impersonation.end" in actions
    starts = [r for r in rows if r["action"] == "impersonation.start"]
    assert starts and starts[0]["actor_user_id"] == po["id"]
    assert starts[0]["acting_as_org_id"] == target["id"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_impersonation_flow.py -v`
Expected: 404 on the routes — blueprint not registered.

- [ ] **Step 3: Write the blueprint**

Create `blueprints/impersonation.py`:
```python
"""Program-owner impersonation: act as <org> for support/testing.

Sets ``session['acting_as_org_id']``; the rest of the app picks it up
via ``core.org_context.effective_org_id()``. Audit-logged on entry and
exit. Real ``user_id`` never changes.
"""
from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, request, session, url_for

from core import audit, org_store
from core.permissions import require_program_owner
from core.org_context import real_user_id

bp = Blueprint("impersonation", __name__)


@bp.route("/admin/organizations/<int:org_id>/impersonate", methods=["POST"])
@require_program_owner
def start(org_id: int):
    org = org_store.get_org_by_id(org_id)
    if org is None:
        abort(404)
    session["acting_as_org_id"] = int(org_id)
    audit.write_event(
        action="impersonation.start",
        actor_user_id=real_user_id(),
        org_id=org_id,
        acting_as_org_id=org_id,
        metadata={"org_name": org.get("name")},
        ip=request.remote_addr,
        ua=request.headers.get("User-Agent"),
    )
    flash(f"Now acting as {org.get('name')}. Exit when finished.", "info")
    return redirect(url_for("admin.organization_detail", org_id=org_id))


@bp.route("/admin/exit-impersonation", methods=["POST"])
@require_program_owner
def end():
    prev = session.pop("acting_as_org_id", None)
    if prev is not None:
        audit.write_event(
            action="impersonation.end",
            actor_user_id=real_user_id(),
            org_id=int(prev),
            acting_as_org_id=int(prev),
            ip=request.remote_addr,
            ua=request.headers.get("User-Agent"),
        )
        flash("Impersonation ended.", "info")
    return redirect(request.referrer or url_for("admin.landing"))
```

- [ ] **Step 4: Register the blueprint in `app.py`**

After the other `app.register_blueprint(admin_bp)` line (~688), add:
```python
    from blueprints.impersonation import bp as impersonation_bp
    app.register_blueprint(impersonation_bp)
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_impersonation_flow.py -v`
Expected: 3 passes.

- [ ] **Step 6: Commit**

```bash
git add blueprints/impersonation.py app.py tests/test_impersonation_flow.py
git commit -m "feat(impersonation): owner can act as <org> with audit trail

POST /admin/organizations/<id>/impersonate sets the session flag,
POST /admin/exit-impersonation clears it. Both write audit_log
rows with actor_user_id=<owner> and acting_as_org_id=<target>.
require_program_owner gate enforces the program-owner-only rule.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4.2: Persistent banner + admin button

**Files:**
- Create: `templates/_impersonation_banner.html`
- Modify: `templates/base.html`
- Modify: `app.py` (extend the existing membership context processor)
- Modify: `templates/admin/organization_detail.html` (add the "Act as this org" button)

- [ ] **Step 1: Create the banner partial**

`templates/_impersonation_banner.html`:
```jinja
{% if impersonating_org %}
<div class="impersonation-banner" role="alert" style="background:#7c2d12;color:#fff;padding:8px 16px;font-weight:600;text-align:center;">
  ⚠ Acting as <strong>{{ impersonating_org.name }}</strong> —
  <form method="post" action="{{ url_for('impersonation.end') }}" style="display:inline">
    <button type="submit" style="background:transparent;border:1px solid #fff;color:#fff;padding:2px 10px;border-radius:4px;cursor:pointer;">Exit impersonation</button>
  </form>
</div>
{% endif %}
```

- [ ] **Step 2: Include from base.html**

Add at the top of `templates/base.html`'s `<body>`:
```jinja
{% include "_impersonation_banner.html" %}
```

- [ ] **Step 3: Extend the membership context processor**

In `app.py:406-...`, add to the dict that the existing `_inject_membership_context` returns:
```python
        # Per-org-creds: when impersonating, surface the target org to the
        # banner template. Returns None when not impersonating; the banner
        # partial renders nothing in that case.
        impersonating_org = None
        acting_id = _flask_session.get("acting_as_org_id")
        if acting_id is not None:
            try:
                from core import org_store as _ostore
                impersonating_org = _ostore.get_org_by_id(int(acting_id))
            except Exception:
                impersonating_org = None
```

And include `impersonating_org` in the returned dict.

(`_flask_session` is `flask.session`; the existing code may already import it as `session`. Use whichever the file already has.)

- [ ] **Step 4: Add the "Act as this org" button**

In `templates/admin/organization_detail.html`, find a place near the org's name (top of the page) and add:
```jinja
{% if not impersonating_org or impersonating_org.id != org.id %}
<form method="post" action="{{ url_for('impersonation.start', org_id=org.id) }}" style="display:inline">
  <button type="submit" class="btn btn-secondary">Act as this org</button>
</form>
{% endif %}
```

- [ ] **Step 5: Manual smoke test**

Run the app locally, log in as program owner, visit `/admin/organizations/<id>`, click the button. The banner should appear on every page until you click Exit.

- [ ] **Step 6: Commit**

```bash
git add templates/_impersonation_banner.html templates/base.html templates/admin/organization_detail.html app.py
git commit -m "feat(impersonation-ui): persistent banner + 'Act as this org' button

Banner partial included from base.html renders on every page when
acting_as_org_id is set, shows the target org name, and offers a
one-click Exit. Admin org-detail page gets the entry-point button.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4.3: Forbidden-during-impersonation guard

**Files:**
- Modify: `blueprints/twofa.py`, `blueprints/recovery.py`, `blueprints/members.py`, and any password-change route in `blueprints/auth.py` or `blueprints/settings.py`
- Test: `tests/test_forbidden_during_impersonation.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_forbidden_during_impersonation.py`:
```python
"""Routes that change account security must 409 under impersonation."""
from __future__ import annotations

import pytest

from core import db, user_store, org_store


FORBIDDEN_ROUTES = [
    # blueprints/twofa.py
    ("POST", "/settings/2fa/enable-totp"),
    ("POST", "/settings/2fa/verify-totp"),
    ("POST", "/settings/2fa/enable-email"),
    ("POST", "/settings/2fa/send-email-code"),
    ("POST", "/settings/2fa/disable"),
    ("POST", "/settings/2fa/recovery-codes/regenerate"),
    # blueprints/settings.py
    ("POST", "/settings/change-password"),
    # blueprints/members.py — role change in the impersonated org
    ("POST", "/settings/members/1/role"),
    # blueprints/recovery.py — admin approval flow
    ("GET",  "/admin-actions/recovery/1/approve"),
    # blueprints/auth.py — own-password set; impersonator must not retarget it
    ("POST", "/login/first-password-set"),
    # Intentionally NOT in this list:
    #   POST /settings/org/require-2fa — org-level toggle the impersonator
    #     may legitimately want to set on behalf of the tenant.
    #   GET  /settings/2fa* (display) — read-only is fine.
]


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SECRET_ENC_KEY", "QcOzn6q5y8yp7v4OoH5sNcWZS_VqIyqU0o8jOwHjW6w=")
    monkeypatch.setenv("FLASK_SECRET_KEY", "t")
    import importlib
    import core.db, core.org_store, core.user_store, core.secrets_store
    importlib.reload(core.db); importlib.reload(core.org_store)
    importlib.reload(core.user_store); importlib.reload(core.secrets_store)
    core.db.init_db()
    import app as m; importlib.reload(m)
    return m.app


@pytest.mark.parametrize("method,path", FORBIDDEN_ROUTES)
def test_route_409s_under_impersonation(app, method, path):
    org = org_store.create_org(name="O", slug="o")
    target = org_store.create_org(name="T", slug="t")
    po = user_store.create_user(
        username="po", email="po@x", password="pw1234567", program_owner=True,
    )
    org_store.add_membership(user_id=po["id"], org_id=org["id"], role="owner")
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = po["id"]
        s["current_org_id"] = org["id"]
        s["acting_as_org_id"] = target["id"]
        s["permission_2fa_passed"] = True
    res = client.open(path, method=method)
    assert res.status_code == 409, f"{method} {path} should 409, got {res.status_code}"
```

- [ ] **Step 2: Apply the decorator**

Add `@forbidden_during_impersonation` (from `core.org_context`) immediately AFTER the route decorator and BEFORE any auth/role guard, in each of:

| File | Function name (search for it) | Route |
|---|---|---|
| `blueprints/twofa.py` | `enable_totp` | `POST /settings/2fa/enable-totp` |
| `blueprints/twofa.py` | `verify_totp` | `POST /settings/2fa/verify-totp` |
| `blueprints/twofa.py` | `enable_email` | `POST /settings/2fa/enable-email` |
| `blueprints/twofa.py` | `send_email_code` | `POST /settings/2fa/send-email-code` |
| `blueprints/twofa.py` | `disable` | `POST /settings/2fa/disable` |
| `blueprints/twofa.py` | `regenerate_recovery_codes` | `POST /settings/2fa/recovery-codes/regenerate` |
| `blueprints/settings.py` | `change_password` (around line 651) | `POST /settings/change-password` |
| `blueprints/members.py` | `change_role` (around line 78) | `POST /settings/members/<id>/role` |
| `blueprints/recovery.py` | `admin_approve` (around line 60) | `GET /admin-actions/recovery/<id>/approve` |
| `blueprints/auth.py` | `first_password_set_post` (around line 361) | `POST /login/first-password-set` |

Example decorator stack:
```python
from core.org_context import forbidden_during_impersonation

@bp.post("/settings/2fa/disable")
@forbidden_during_impersonation
def disable():
    ...
```

The decorator stack order matters: `forbidden_during_impersonation` runs BEFORE the auth/role gate so impersonation is caught even if the gate would have passed.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_forbidden_during_impersonation.py -v`
Expected: all parameterized cases pass.

- [ ] **Step 5: Commit**

```bash
git add blueprints/twofa.py blueprints/recovery.py blueprints/members.py blueprints/auth.py blueprints/settings.py tests/test_forbidden_during_impersonation.py
git commit -m "feat(impersonation): block account-security mutations under impersonation

2FA enable/disable, recovery approval, role changes, and password
changes return 409 with a helpful message while
acting_as_org_id is set. The program owner must Exit
impersonation before they can touch those routes on a tenant's
behalf.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Phase 5: Agent dispatch + legacy migration

### Task 5.1: agent_dispatch.collect_credentials per-org + job_plan org_id

**Files:**
- Modify: `core/agent_dispatch.py:36-63, 200-235, 349-411`
- Test: `tests/test_agent_dispatch_org_scope.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_dispatch_org_scope.py`:
```python
"""agent_dispatch ships the effective org's credentials, not the legacy slot."""
from __future__ import annotations

import pytest
from flask import Flask

from core import db, secrets_store
from core import agent_dispatch


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    import importlib
    importlib.reload(db); db.init_db()
    yield


@pytest.fixture()
def app_ctx():
    app = Flask(__name__); app.secret_key = "t"
    with app.test_request_context():
        yield


def test_collect_credentials_pulls_from_effective_org(app_ctx):
    from flask import session
    secrets_store.set_secret("youtube.token", "tok-A", org_id=1)
    secrets_store.set_secret("youtube.token", "tok-B", org_id=2)
    secrets_store.set_platform_secret("youtube.client_secrets", "{}")
    session["current_org_id"] = 1
    creds = agent_dispatch.collect_credentials(
        platforms_in_use={"YouTube Video"},
    )
    assert creds["youtube.token"] == "tok-A"
    assert creds["youtube.client_secrets"] == "{}"


def test_collect_credentials_under_impersonation(app_ctx):
    from flask import session
    secrets_store.set_secret("youtube.token", "tok-A", org_id=1)
    secrets_store.set_secret("youtube.token", "tok-B", org_id=2)
    secrets_store.set_platform_secret("youtube.client_secrets", "{}")
    session["current_org_id"] = 1
    session["acting_as_org_id"] = 2
    creds = agent_dispatch.collect_credentials(
        platforms_in_use={"YouTube Video"},
    )
    assert creds["youtube.token"] == "tok-B"


def test_envelope_carries_org_id(app_ctx):
    from flask import session
    session["current_org_id"] = 7
    env = agent_dispatch.build_envelope(
        job_id="job1", rows=[], entries={}, credentials={}, config={},
    )
    assert env["payload"]["org_id"] == 7
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_agent_dispatch_org_scope.py -v`
Expected: failures.

- [ ] **Step 3: Update `collect_credentials`**

In `core/agent_dispatch.py:36-63`, change `_fetch_credential` and `collect_credentials`:
```python
def _fetch_credential(key: str, *, org_id: int | None) -> str | None:
    """Return the credential string for *key* at the right scope.

    youtube.client_secrets is platform-shared; everything else is per-org.
    kv first; blob fallback.
    """
    if key == "youtube.client_secrets":
        val = _ss.get_platform_secret(key)
        if val is not None:
            return val
        raw = _ss.get_platform_blob(key)
        return None if raw is None else raw.decode("utf-8")
    val = _ss.get_secret(key, org_id=org_id)
    if val is not None:
        return val
    raw = _ss.get_blob(key, org_id=org_id)
    return None if raw is None else raw.decode("utf-8")


def collect_credentials(*, platforms_in_use: set[str],
                        org_id: int | None = None) -> dict[str, str]:
    """Return only the secrets_store entries needed for the given platforms.

    *org_id* defaults to ``effective_org_id()`` so request-context callers
    can omit it. Missing keys are silently omitted (intentional — the
    agent surfaces a per-platform error if it later finds a key absent).
    """
    if org_id is None:
        from core.org_context import effective_org_id
        org_id = effective_org_id()
    needed: set[str] = set()
    for p in platforms_in_use:
        needed.update(_PLATFORM_KEYS.get(p, ()))
    out: dict[str, str] = {}
    for key in sorted(needed):
        val = _fetch_credential(key, org_id=org_id)
        if val is not None:
            out[key] = val
    return out
```

- [ ] **Step 4: Embed `org_id` in the envelope**

Find `build_envelope` (around line 204) and ensure the payload includes `org_id`:
```python
def build_envelope(
    *, job_id: str, rows: list[dict], entries: dict,
    credentials: dict[str, str], config: dict,
    org_id: int | None = None,
) -> dict:
    if org_id is None:
        from core.org_context import effective_org_id
        org_id = effective_org_id()
    return {
        "v": _PROTOCOL_VERSION,
        "type": "job_plan",
        "job_id": job_id,
        "payload": {
            "org_id": org_id,
            "rows": rows,
            "entries": entries,
            "credentials": credentials,
            "config": config,
        },
    }
```

- [ ] **Step 5: `start()` passes org through**

In `core/agent_dispatch.py:349-411`, change:
```python
def start(*, session_id, summary, entries, elements, config,
          device_id=None, browser_ip=None) -> str:
    from core.org_context import effective_org_id
    org_id = effective_org_id()
    job_id = _uuid.uuid4().hex
    rows = filter_done_rows(session_id=session_id, summary=summary)
    if not rows:
        _logger.info("agent_dispatch.start(job=%s): nothing to do", job_id)
        return job_id
    for r in rows:
        r["elements"] = elements.get(r["iso_date"], {})
    platforms_in_use: set[str] = set()
    for r in rows:
        platforms_in_use.update(r["platforms"])
    creds = collect_credentials(platforms_in_use=platforms_in_use, org_id=org_id)
    envelope = build_envelope(
        job_id=job_id, rows=rows, entries=entries,
        credentials=creds, config=config, org_id=org_id,
    )
    # ... rest unchanged
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_agent_dispatch_org_scope.py tests/test_agent_dispatch.py -v`
Expected: all pass; `test_agent_dispatch.py` may need request-context setup added.

- [ ] **Step 7: Commit**

```bash
git add core/agent_dispatch.py tests/test_agent_dispatch_org_scope.py tests/test_agent_dispatch.py
git commit -m "feat(agent-dispatch): per-org creds + org_id in job_plan envelope

collect_credentials reads from effective_org_id() (per-tenant for
youtube.token and Playwright session blobs; platform-shared for
youtube.client_secrets). The job_plan envelope carries org_id so
the agent and the relay both know which tenant the run belongs to.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5.2: Legacy secrets migration

**Files:**
- Modify: `core/migration_bootstrap.py:43-50` (and append a new helper)
- Test: `tests/test_legacy_secret_migration.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_legacy_secret_migration.py`:
```python
"""migration_bootstrap moves legacy unscoped secrets to the bootstrap org's scope."""
from __future__ import annotations

import pytest

from core import db, secrets_store, migration_bootstrap, org_store, user_store


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SECRET_ENC_KEY", "QcOzn6q5y8yp7v4OoH5sNcWZS_VqIyqU0o8jOwHjW6w=")
    monkeypatch.setenv("PROGRAM_OWNER_EMAIL", "po@x")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "pw1234567")
    import importlib
    import core.db, core.org_store, core.user_store, core.secrets_store
    importlib.reload(core.db); importlib.reload(core.org_store)
    importlib.reload(core.user_store); importlib.reload(core.secrets_store)
    core.db.init_db()


def test_legacy_kv_secret_moves_to_bootstrap_org(env):
    secrets_store.set_secret("UNSPLASH_ACCESS_KEY", "k-legacy")
    migration_bootstrap.run_migration()
    org = org_store.get_org_by_slug("lcbc-church")
    assert secrets_store.get_secret("UNSPLASH_ACCESS_KEY", org_id=org["id"]) == "k-legacy"
    assert secrets_store.get_secret("UNSPLASH_ACCESS_KEY") is None


def test_legacy_blob_moves_to_bootstrap_org(env):
    secrets_store.set_blob("playwright.simplecast_session", b"sess-bytes")
    migration_bootstrap.run_migration()
    org = org_store.get_org_by_slug("lcbc-church")
    assert secrets_store.get_blob("playwright.simplecast_session", org_id=org["id"]) == b"sess-bytes"


def test_client_secrets_moves_to_platform_scope(env):
    secrets_store.set_blob("youtube.client_secrets", b'{"web":{}}')
    migration_bootstrap.run_migration()
    assert secrets_store.get_platform_blob("youtube.client_secrets") == b'{"web":{}}'
    assert secrets_store.get_blob("youtube.client_secrets") is None


def test_password_hash_stays_unscoped(env):
    from core.auth import _HASH_SECRET
    secrets_store.set_secret(_HASH_SECRET, "$argon2id$...")
    migration_bootstrap.run_migration()
    # Legacy slot intact, NOT moved to org scope.
    assert secrets_store.get_secret(_HASH_SECRET) is not None


def test_migration_is_idempotent(env):
    secrets_store.set_secret("UNSPLASH_ACCESS_KEY", "k-legacy")
    migration_bootstrap.run_migration()
    migration_bootstrap.run_migration()  # second time = no-op
    org = org_store.get_org_by_slug("lcbc-church")
    assert secrets_store.get_secret("UNSPLASH_ACCESS_KEY", org_id=org["id"]) == "k-legacy"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_legacy_secret_migration.py -v`
Expected: failures — migration doesn't yet rewrite storage names.

- [ ] **Step 3: Add `_migrate_legacy_secret_names` to migration_bootstrap**

Replace the existing `_backfill_secrets` (`core/migration_bootstrap.py:43-50`) with two functions:
```python
def _backfill_secrets(org_id: int) -> int:
    """Legacy: stamp org_id column on rows that have a NULL value.

    Predates the storage-name migration; left in place for the
    handful of rows that may have an unscoped storage name AND a
    NULL org_id column. The new _migrate_legacy_secret_names below
    is the real per-org isolation step.
    """
    with db._get_conn() as c:
        cur = c.execute(
            "UPDATE secrets SET org_id=? WHERE org_id IS NULL",
            (org_id,),
        )
        c.commit()
        return cur.rowcount


def _migrate_legacy_secret_names(org_id: int) -> dict[str, int]:
    """Rewrite legacy unscoped storage names into org: + platform: scopes.

    Idempotent: any row that already lives under org:<id>:... or
    platform:... is left alone. The legacy password-hash row
    (core.auth._HASH_SECRET) stays unscoped — it's not a tenant secret.

    Returns a counter dict {"moved_to_org": N, "moved_to_platform": M}.
    """
    from core.auth import _HASH_SECRET
    from uploaders.youtube_uploader import _YT_CLIENT_SECRETS_NAME
    moved_to_org = 0
    moved_to_platform = 0
    with db._get_conn() as c:
        rows = c.execute(
            "SELECT name, kind, value, updated_at FROM secrets"
        ).fetchall()
        for row in rows:
            name = row["name"]
            if name.startswith("org:") or name.startswith("platform:"):
                continue
            if name == _HASH_SECRET:
                continue
            if name == _YT_CLIENT_SECRETS_NAME:
                new_name = f"platform:{name}"
                moved_to_platform += 1
            else:
                new_name = f"org:{org_id}:{name}"
                moved_to_org += 1
            # Use the original encrypted value verbatim — we don't decrypt
            # and re-encrypt, that's an unnecessary key-rotation surface.
            c.execute(
                "INSERT OR REPLACE INTO secrets "
                "(name, kind, value, updated_at, org_id) "
                "VALUES (?,?,?,?,?)",
                (new_name, row["kind"], row["value"], row["updated_at"],
                 None if new_name.startswith("platform:") else org_id),
            )
            c.execute("DELETE FROM secrets WHERE name=?", (name,))
        c.commit()
    return {"moved_to_org": moved_to_org, "moved_to_platform": moved_to_platform}
```

- [ ] **Step 4: Call it from `run_migration`**

In `core/migration_bootstrap.py:127-133`, after the existing backfill calls:
```python
    # Backfill legacy rows.
    d = _backfill_devices(user_id)
    s = _backfill_secrets(org["id"])
    h = _backfill_upload_history(org["id"], user_id)
    legacy = _migrate_legacy_secret_names(org["id"])
    log.info(
        "Migration: backfilled %d device rows, %d secret rows, %d history rows; "
        "moved %d legacy secrets to org scope and %d to platform scope.",
        d, s, h, legacy["moved_to_org"], legacy["moved_to_platform"],
    )
```

If you want an audit row for the rewrite:
```python
    if legacy["moved_to_org"] or legacy["moved_to_platform"]:
        from core import audit
        audit.write_event(
            action="system.legacy_secret_migration",
            actor_user_id=None,
            org_id=org["id"],
            metadata=legacy,
        )
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_legacy_secret_migration.py -v`
Expected: 5 passes.

- [ ] **Step 6: Run full migration tests**

Run: `python -m pytest tests/test_migration_bootstrap.py tests/test_legacy_secret_migration.py -v`
Expected: all pass; the existing migration test may need an update if it inspects legacy rows that the new step would relocate.

- [ ] **Step 7: Commit**

```bash
git add core/migration_bootstrap.py tests/test_legacy_secret_migration.py tests/test_migration_bootstrap.py
git commit -m "feat(migration): rewrite legacy unscoped secret names into org/platform scope

run_migration() now relocates every legacy unscoped row into
either org:<bootstrap>:<name> or platform:<name> (for the GCP
client_secrets blob). Re-encryption is avoided — the existing
Fernet value is moved verbatim. Idempotent: second run is a no-op.
Audit-logged once via system.legacy_secret_migration when rows
were actually moved.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Phase 6: Cross-org isolation acceptance

### Task 6.1: Cross-org credential isolation integration test

**Files:**
- Create: `tests/integration/test_cross_org_isolation.py`

- [ ] **Step 1: Write the test**

```python
"""End-to-end: org A and org B never see each other's credentials.

Boots a real Flask app, creates two orgs, populates each with its own
youtube.token, and verifies the uploader's loader returns the right
token for the active session each time.
"""
from __future__ import annotations

import json
import pytest

from core import db, user_store, org_store, secrets_store


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SECRET_ENC_KEY", "QcOzn6q5y8yp7v4OoH5sNcWZS_VqIyqU0o8jOwHjW6w=")
    monkeypatch.setenv("FLASK_SECRET_KEY", "t")
    import importlib
    import core.db, core.org_store, core.user_store, core.secrets_store
    importlib.reload(core.db); importlib.reload(core.org_store)
    importlib.reload(core.user_store); importlib.reload(core.secrets_store)
    core.db.init_db()
    import app as m; importlib.reload(m)
    return m.app


def test_two_orgs_two_tokens_no_leakage(app):
    org_a = org_store.create_org(name="A", slug="a")
    org_b = org_store.create_org(name="B", slug="b")
    secrets_store.set_secret("youtube.token", '{"t":"A"}', org_id=org_a["id"])
    secrets_store.set_secret("youtube.token", '{"t":"B"}', org_id=org_b["id"])
    secrets_store.set_platform_secret("youtube.client_secrets", "{}")

    user_a = user_store.create_user(
        username="ua", email="ua@x", password="pw1234567", program_owner=False,
    )
    user_b = user_store.create_user(
        username="ub", email="ub@x", password="pw1234567", program_owner=False,
    )
    org_store.add_membership(user_id=user_a["id"], org_id=org_a["id"], role="owner")
    org_store.add_membership(user_id=user_b["id"], org_id=org_b["id"], role="owner")

    from flask import session as flask_session
    from uploaders import youtube_uploader as yt
    with app.test_request_context():
        flask_session["user_id"] = user_a["id"]
        flask_session["current_org_id"] = org_a["id"]
        assert json.loads(yt._load_token_json())["t"] == "A"
    with app.test_request_context():
        flask_session["user_id"] = user_b["id"]
        flask_session["current_org_id"] = org_b["id"]
        assert json.loads(yt._load_token_json())["t"] == "B"


def test_owner_impersonating_reads_target_org_token(app):
    org_t = org_store.create_org(name="Target", slug="t")
    secrets_store.set_secret("youtube.token", '{"t":"target"}', org_id=org_t["id"])
    secrets_store.set_platform_secret("youtube.client_secrets", "{}")
    po_org = org_store.create_org(name="Boot", slug="boot")
    po = user_store.create_user(
        username="po", email="po@x", password="pw1234567", program_owner=True,
    )
    org_store.add_membership(user_id=po["id"], org_id=po_org["id"], role="owner")
    from flask import session as flask_session
    from uploaders import youtube_uploader as yt
    with app.test_request_context():
        flask_session["user_id"] = po["id"]
        flask_session["current_org_id"] = po_org["id"]
        flask_session["acting_as_org_id"] = org_t["id"]
        assert json.loads(yt._load_token_json())["t"] == "target"
```

- [ ] **Step 2: Run**

```bash
python -m pytest tests/integration/test_cross_org_isolation.py -v
```
Expected: 2 passes.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_cross_org_isolation.py
git commit -m "test(cross-org-isolation): end-to-end credential isolation + impersonation

Two orgs with distinct youtube.token values produce two distinct
loader results based on session['current_org_id'].
acting_as_org_id correctly overrides current_org_id during
impersonation.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 6.2: Push + watch CI + tag

**Files:**
- None (operations only)

- [ ] **Step 1: Push all phase-6 work**

```bash
git push origin main
```

- [ ] **Step 2: Watch CI**

```bash
gh run watch --exit-status
```
Expected: green. Note `Run tests` should now also enforce the secret-scoping lint.

- [ ] **Step 3: Confirm CI summary**

```bash
gh run list --branch main --limit 1
```
Expected: `success`.

- [ ] **Step 4: Decide on agent tag**

This work touched agent dispatch (`core/agent_dispatch.py`) but no agent-side files (`agent/*`). So:
- If only `core/`, `blueprints/`, `templates/`, `tests/` changed: no new `agent-v*` tag needed. The deployed agent v0.6.15 keeps working because the server-side envelope is a superset (`org_id` field added; agent ignores unknown fields).
- If you did touch `agent/*`: bump `agent/_version.py`, tag `agent-v0.6.16`, push the tag, wait for the release-agent workflow to publish binaries, then SCP/HTTP-pull them onto the VPS as in the previous turn.

For this plan, no agent code changes are required. **Skip the tag.**

- [ ] **Step 5: Deploy the server-side change to the VPS**

The Flask app on `dropshippa` needs the new code + the schema migration. Discover the actual compose service name first:
```bash
wsl ssh dropshippa "cd /root/DailyLifeDistributor && docker compose ps"
```
Then pull + restart that service. Example (replace `<svc>` with the service name shown):
```bash
wsl ssh dropshippa "cd /root/DailyLifeDistributor && git pull && docker compose restart <svc>"
```

Verify the migration ran cleanly:
```bash
wsl ssh dropshippa "cd /root/DailyLifeDistributor && docker compose logs --tail=200 <svc> | grep -i 'Migration:'"
```
Expected log lines: `Migration: backfilled N device rows, M secret rows, K history rows; moved X legacy secrets to org scope and Y to platform scope.`

- [ ] **Step 6: Smoke-test in production**

Visit `https://autoalert.pro` as program owner:
1. `/admin/organizations/<bootstrap-org>` — click "Act as this org" — banner appears.
2. `/settings` — secret rows shown reflect the bootstrap org's keys (since you're acting as it).
3. `/admin/exit-impersonation` — banner disappears.
4. Try `POST /settings/2fa/disable` while impersonating — expect 409 (Postman or `curl`-ish).

Document anything weird in `MEMORY.md` if it warrants future-me's attention.

---

## Self-review checklist

1. **Spec coverage:** Every section of `2026-05-24-per-org-credentials-design.md` has at least one task above. ✓
2. **Idempotency:** Migration runs are explicitly tested twice in Task 5.2 step 1 (`test_migration_is_idempotent`).
3. **Audit shape:** Task 1.3 + 4.1 ensure `acting_as_org_id` is filled (auto + explicit) on every relevant write.
4. **Forbidden routes:** Task 4.3 documents the parametrized test and asks the engineer to extend the list during Step 2's grep.
5. **No agent-side code:** Task 5.1 explicitly notes the agent shim is by-value and needs no changes.
6. **Lint progression:** Task 2.2 wires the lint as `continue-on-error`, Task 3.4 flips it to required — the engineer is held accountable to actually finish plumbing.
7. **Single-tenant USB path preserved:** Task 3.1 keeps the on-disk `client_secrets.json` fallback for `LEGACY_PASSWORD_ENABLED=true` installs.

End of plan.
