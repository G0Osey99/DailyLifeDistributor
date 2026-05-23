# Multi-Tenant Phase α — Schema + Users + Orgs + Auth + Admin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay the multi-tenant foundation: schema for users/orgs/memberships (+ forward-declared tables for later phases), Argon2id password auth (with legacy shared-password fallback gated by env flag), Resend integration wired but no live emails sent yet, program-owner admin pages, and an idempotent migration that creates the "LCBC Church" org from existing single-tenant data.

**Architecture:** Schema changes are additive and backward-compatible (nullable FKs let legacy code keep working). New `core/user_store.py` and `core/org_store.py` modules hold the new CRUD; `core/auth.py` is rewritten to key sessions by `user_id` + `current_org_id` while preserving the old `authenticated` boolean for one release. `blueprints/admin.py` is new and gated by `users.program_owner = TRUE`. `core/migration_bootstrap.py` runs at app startup and is idempotent.

**Tech Stack:** Python 3.11+, SQLite, Flask, argon2-cffi, itsdangerous, resend, Flask-WTF for CSRF, pytest.

**Spec:** `docs/superpowers/specs/2026-05-23-multi-tenant-architecture-design.md`

---

## File Structure

### New files
- `core/user_store.py` — Argon2id password hash + verify; user CRUD (create_user, get_user_by_id, get_user_by_username, get_user_by_email, verify_password, update_password, update_last_login_at).
- `core/org_store.py` — Organization + membership CRUD (create_org, get_org_by_id, get_org_by_slug, list_orgs, add_membership, get_membership, list_memberships_for_user, list_members_of_org, change_role, remove_membership).
- `core/email.py` — Resend wrapper; `render_template(name, **vars) -> (subject, html, text)` and `send(name, to, **vars)`; no-op + WARNING when `RESEND_API_KEY` unset.
- `core/permissions.py` — `require_program_owner` view decorator (403 if `session["user_id"]` not flagged program_owner).
- `core/migration_bootstrap.py` — Idempotent `run_migration()` that seeds LCBC Church org + bootstrap program-owner user + backfills `agent_devices.user_id`, `secrets.org_id`, `upload_history.{org_id,user_id}`.
- `blueprints/admin.py` — Program-owner admin: `/admin`, `/admin/organizations` (GET list, POST create), `/admin/users` (GET list, POST force-reset).
- `templates/admin/organizations.html` — Org list + create form.
- `templates/admin/users.html` — User list + force-reset form.
- `templates/email/welcome.html`, `templates/email/welcome.txt` — welcome template stub (filled out in PR-β).
- `tests/test_user_store.py` — Argon2id round-trip + verify_password edge cases.
- `tests/test_org_store.py` — Org + membership CRUD + slug uniqueness.
- `tests/test_email.py` — Render path + no-op behavior when key missing.
- `tests/test_blueprints_admin.py` — Permission gate + org create + user list.
- `tests/test_migration_bootstrap.py` — Idempotent re-run + LCBC backfill.
- `tests/test_login_argon2.py` — POST /login with username + password flow.

### Modified files
- `requirements.txt` — add argon2-cffi, pyotp, qrcode[pil], resend, itsdangerous, flask-wtf.
- `requirements-dev.txt` — add freezegun.
- `core/db.py` — add 7 CREATE TABLEs (organizations, users, org_memberships, invitations, recovery_codes, recovery_requests, audit_log, audit_log_archive) and 3 ALTERs (agent_devices.user_id, secrets.org_id, upload_history.{org_id,user_id}). All idempotent via PRAGMA table_info check.
- `core/auth.py` — keep shared-password path behind `LEGACY_PASSWORD_ENABLED`; new helpers `current_user_id()`, `current_org_id()`, `is_authenticated()` resolves to `session["user_id"] is not None`.
- `blueprints/auth.py` — POST /login now accepts username + password and dispatches: if user found → Argon2id verify → session["user_id"], session["current_org_id"] = first membership's org_id; legacy form preserved when `LEGACY_PASSWORD_ENABLED=true`.
- `templates/login.html` — username + password fields; legacy single-field form rendered when `legacy_enabled` template var is truthy.
- `templates/base.html` — switch-org dropdown in header when len(memberships) > 1.
- `app.py` — call `core.migration_bootstrap.run_migration()` after `init_db()`, before serving.

---

### Task 1: Add multi-tenant runtime dependencies

- [ ] **Step 1.1:** Write a failing test for the version pins.

  Create `tests/test_requirements_pins.py`:
  ```python
  """Ensures the multi-tenant deps are present in requirements.txt."""
  from pathlib import Path

  REQ = Path(__file__).resolve().parent.parent / "requirements.txt"

  def test_argon2_cffi_pinned():
      contents = REQ.read_text(encoding="utf-8")
      assert "argon2-cffi>=23" in contents, "argon2-cffi must be pinned >=23"

  def test_pyotp_pinned():
      contents = REQ.read_text(encoding="utf-8")
      assert "pyotp>=2.9" in contents

  def test_qrcode_pinned():
      contents = REQ.read_text(encoding="utf-8")
      assert "qrcode[pil]>=7.4" in contents

  def test_resend_pinned():
      contents = REQ.read_text(encoding="utf-8")
      assert "resend>=0.7" in contents

  def test_itsdangerous_pinned():
      contents = REQ.read_text(encoding="utf-8")
      assert "itsdangerous>=2.1" in contents

  def test_flask_wtf_pinned():
      contents = REQ.read_text(encoding="utf-8")
      assert "flask-wtf>=1.2" in contents
  ```

- [ ] **Step 1.2:** Run the test, confirm it fails.

  ```bash
  pytest tests/test_requirements_pins.py -q
  ```

- [ ] **Step 1.3:** Append the new pins to `requirements.txt`:
  ```text
  # Multi-tenant phase α: password hashing, 2FA (forward-declared for PR-γ),
  # transactional email, signed tokens, CSRF.
  argon2-cffi>=23,<24
  pyotp>=2.9,<3
  qrcode[pil]>=7.4,<8
  resend>=0.7,<3
  itsdangerous>=2.1,<3
  flask-wtf>=1.2,<2
  ```

- [ ] **Step 1.4:** Install the new deps locally.

  ```bash
  python -m pip install -r requirements.txt
  ```

- [ ] **Step 1.5:** Re-run tests; confirm pass.

  ```bash
  pytest tests/test_requirements_pins.py -q
  ```

- [ ] **Step 1.6:** Commit.

  ```bash
  git add requirements.txt tests/test_requirements_pins.py
  git commit -m "deps(α): pin argon2-cffi, pyotp, qrcode, resend, itsdangerous, flask-wtf"
  ```

---

### Task 2: Add freezegun for time-sensitive tests

- [ ] **Step 2.1:** Write a failing test.

  Append to `tests/test_requirements_pins.py`:
  ```python
  def test_freezegun_pinned():
      dev = (Path(__file__).resolve().parent.parent / "requirements-dev.txt").read_text(encoding="utf-8")
      assert "freezegun>=1.4" in dev
  ```

- [ ] **Step 2.2:** Run — fails.

  ```bash
  pytest tests/test_requirements_pins.py::test_freezegun_pinned -q
  ```

- [ ] **Step 2.3:** Append to `requirements-dev.txt`:
  ```text
  # Multi-tenant phase α: freeze datetime.now() for password_changed_at /
  # invitations.expires_at / recovery_requests.expires_at deterministic tests.
  freezegun>=1.4,<2
  ```

- [ ] **Step 2.4:** Install.

  ```bash
  python -m pip install -r requirements-dev.txt
  ```

- [ ] **Step 2.5:** Re-run; pass.

  ```bash
  pytest tests/test_requirements_pins.py -q
  ```

- [ ] **Step 2.6:** Commit.

  ```bash
  git add requirements-dev.txt tests/test_requirements_pins.py
  git commit -m "deps(α): pin freezegun for time-sensitive tests"
  ```

---

### Task 3: CREATE TABLE organizations (idempotent)

- [ ] **Step 3.1:** Write a failing test.

  Create `tests/test_schema_organizations.py`:
  ```python
  """organizations table schema + idempotent migration."""
  import sqlite3
  from core import db

  def _cols(table: str) -> set[str]:
      with db._get_conn() as c:
          return {r[1] for r in c.execute(f"PRAGMA table_info('{table}')").fetchall()}

  def test_organizations_table_created():
      db.init_db()
      assert "organizations" in {
          r[0] for r in db._get_conn().__enter__().execute(
              "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
      }

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
  ```

- [ ] **Step 3.2:** Run — fails (table missing).

  ```bash
  pytest tests/test_schema_organizations.py -q
  ```

- [ ] **Step 3.3:** In `core/db.py`, inside `init_db()` after the `secrets` CREATE TABLE block and before the existing idempotent ALTERs, insert:
  ```python
          # Multi-tenant phase α: organizations.
          conn.execute("""
              CREATE TABLE IF NOT EXISTS organizations (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  slug TEXT NOT NULL UNIQUE,
                  plan TEXT NOT NULL DEFAULT 'free',
                  billing_email TEXT,
                  require_2fa INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  created_by_user_id INTEGER,
                  disabled_at TEXT
              )
          """)
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_orgs_slug ON organizations(slug)"
          )
  ```

- [ ] **Step 3.4:** Re-run; pass.

  ```bash
  pytest tests/test_schema_organizations.py -q
  ```

- [ ] **Step 3.5:** Commit.

  ```bash
  git add core/db.py tests/test_schema_organizations.py
  git commit -m "schema(α): organizations table (idempotent CREATE)"
  ```

---

### Task 4: CREATE TABLE users (idempotent)

- [ ] **Step 4.1:** Failing test.

  Create `tests/test_schema_users.py`:
  ```python
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
  ```

- [ ] **Step 4.2:** Run — fails.

  ```bash
  pytest tests/test_schema_users.py -q
  ```

- [ ] **Step 4.3:** In `core/db.py`, after the organizations block:
  ```python
          conn.execute("""
              CREATE TABLE IF NOT EXISTS users (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT NOT NULL UNIQUE,
                  email TEXT NOT NULL UNIQUE,
                  password_hash TEXT NOT NULL,
                  totp_secret_encrypted TEXT,
                  email_2fa_enabled INTEGER NOT NULL DEFAULT 0,
                  program_owner INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  last_login_at TEXT,
                  password_changed_at TEXT
              )
          """)
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)"
          )
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)"
          )
  ```

- [ ] **Step 4.4:** Re-run; pass.

  ```bash
  pytest tests/test_schema_users.py -q
  ```

- [ ] **Step 4.5:** Commit.

  ```bash
  git add core/db.py tests/test_schema_users.py
  git commit -m "schema(α): users table (Argon2id password_hash, program_owner flag)"
  ```

---

### Task 5: CREATE TABLE org_memberships

- [ ] **Step 5.1:** Failing test.

  Create `tests/test_schema_memberships.py`:
  ```python
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
  ```

- [ ] **Step 5.2:** Run — fails.

  ```bash
  pytest tests/test_schema_memberships.py -q
  ```

- [ ] **Step 5.3:** Add to `core/db.py`:
  ```python
          conn.execute("""
              CREATE TABLE IF NOT EXISTS org_memberships (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  org_id INTEGER NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('owner','manager','user')),
                  joined_at TEXT NOT NULL,
                  UNIQUE(user_id, org_id),
                  FOREIGN KEY(user_id) REFERENCES users(id),
                  FOREIGN KEY(org_id) REFERENCES organizations(id)
              )
          """)
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_memberships_user "
              "ON org_memberships(user_id)"
          )
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_memberships_org "
              "ON org_memberships(org_id)"
          )
  ```

- [ ] **Step 5.4:** Run; pass.

  ```bash
  pytest tests/test_schema_memberships.py -q
  ```

- [ ] **Step 5.5:** Commit.

  ```bash
  git add core/db.py tests/test_schema_memberships.py
  git commit -m "schema(α): org_memberships (role CHECK, unique(user,org))"
  ```

---

### Task 6: CREATE TABLE invitations (forward-declared for PR-β)

- [ ] **Step 6.1:** Failing test.

  Create `tests/test_schema_invitations.py`:
  ```python
  from core import db

  def test_invitations_columns():
      db.init_db()
      with db._get_conn() as c:
          cols = {r[1] for r in c.execute(
              "PRAGMA table_info('invitations')").fetchall()}
      assert {"id", "org_id", "inviter_user_id", "email", "role",
              "token_hash", "expires_at", "accepted_at", "revoked_at",
              "created_at"} <= cols
  ```

- [ ] **Step 6.2:** Run — fails.

  ```bash
  pytest tests/test_schema_invitations.py -q
  ```

- [ ] **Step 6.3:** Add to `core/db.py`:
  ```python
          conn.execute("""
              CREATE TABLE IF NOT EXISTS invitations (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  org_id INTEGER NOT NULL,
                  inviter_user_id INTEGER NOT NULL,
                  email TEXT NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('owner','manager','user')),
                  token_hash TEXT NOT NULL UNIQUE,
                  expires_at TEXT NOT NULL,
                  accepted_at TEXT,
                  revoked_at TEXT,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(org_id) REFERENCES organizations(id),
                  FOREIGN KEY(inviter_user_id) REFERENCES users(id)
              )
          """)
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_invitations_email "
              "ON invitations(email)"
          )
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_invitations_org "
              "ON invitations(org_id)"
          )
  ```

- [ ] **Step 6.4:** Pass + commit.

  ```bash
  pytest tests/test_schema_invitations.py -q
  git add core/db.py tests/test_schema_invitations.py
  git commit -m "schema(α): invitations table (forward-declared for PR-β)"
  ```

---

### Task 7: CREATE TABLE recovery_codes + recovery_requests (forward for PR-γ)

- [ ] **Step 7.1:** Failing test.

  Create `tests/test_schema_recovery.py`:
  ```python
  from core import db

  def test_recovery_codes_columns():
      db.init_db()
      with db._get_conn() as c:
          cols = {r[1] for r in c.execute(
              "PRAGMA table_info('recovery_codes')").fetchall()}
      assert {"id", "user_id", "code_hash", "used_at", "created_at"} <= cols

  def test_recovery_requests_columns():
      db.init_db()
      with db._get_conn() as c:
          cols = {r[1] for r in c.execute(
              "PRAGMA table_info('recovery_requests')").fetchall()}
      assert {"id", "user_id", "requested_at", "expires_at",
              "approver_user_id", "approved_at",
              "password_reset_token_hash", "consumed_at"} <= cols
  ```

- [ ] **Step 7.2:** Run — fails.

  ```bash
  pytest tests/test_schema_recovery.py -q
  ```

- [ ] **Step 7.3:** Add to `core/db.py`:
  ```python
          conn.execute("""
              CREATE TABLE IF NOT EXISTS recovery_codes (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  code_hash TEXT NOT NULL,
                  used_at TEXT,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(user_id) REFERENCES users(id)
              )
          """)
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_recovery_codes_user "
              "ON recovery_codes(user_id)"
          )
          conn.execute("""
              CREATE TABLE IF NOT EXISTS recovery_requests (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  requested_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  approver_user_id INTEGER,
                  approved_at TEXT,
                  password_reset_token_hash TEXT,
                  consumed_at TEXT,
                  FOREIGN KEY(user_id) REFERENCES users(id),
                  FOREIGN KEY(approver_user_id) REFERENCES users(id)
              )
          """)
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_recovery_requests_user "
              "ON recovery_requests(user_id)"
          )
  ```

- [ ] **Step 7.4:** Pass + commit.

  ```bash
  pytest tests/test_schema_recovery.py -q
  git add core/db.py tests/test_schema_recovery.py
  git commit -m "schema(α): recovery_codes + recovery_requests (forward for PR-γ)"
  ```

---

### Task 8: CREATE TABLE audit_log + audit_log_archive (forward for PR-γ)

- [ ] **Step 8.1:** Failing test.

  Create `tests/test_schema_audit.py`:
  ```python
  from core import db

  def _cols(t):
      with db._get_conn() as c:
          return {r[1] for r in c.execute(f"PRAGMA table_info('{t}')").fetchall()}

  def test_audit_log_columns():
      db.init_db()
      cols = _cols("audit_log")
      assert {"id", "org_id", "actor_user_id", "action",
              "target_type", "target_id", "metadata", "ip",
              "user_agent", "created_at"} <= cols

  def test_audit_log_archive_mirrors():
      db.init_db()
      assert _cols("audit_log") == _cols("audit_log_archive")
  ```

- [ ] **Step 8.2:** Run — fails.

  ```bash
  pytest tests/test_schema_audit.py -q
  ```

- [ ] **Step 8.3:** Add to `core/db.py`:
  ```python
          for _t in ("audit_log", "audit_log_archive"):
              conn.execute(f"""
                  CREATE TABLE IF NOT EXISTS {_t} (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      org_id INTEGER,
                      actor_user_id INTEGER,
                      action TEXT NOT NULL,
                      target_type TEXT,
                      target_id INTEGER,
                      metadata TEXT,
                      ip TEXT,
                      user_agent TEXT,
                      created_at TEXT NOT NULL
                  )
              """)
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_audit_org_time "
              "ON audit_log(org_id, created_at)"
          )
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_audit_actor_time "
              "ON audit_log(actor_user_id, created_at)"
          )
  ```

- [ ] **Step 8.4:** Pass + commit.

  ```bash
  pytest tests/test_schema_audit.py -q
  git add core/db.py tests/test_schema_audit.py
  git commit -m "schema(α): audit_log + audit_log_archive (forward for PR-γ)"
  ```

---

### Task 9: ALTER agent_devices ADD COLUMN user_id

- [ ] **Step 9.1:** Failing test.

  Create `tests/test_schema_agent_devices_user_id.py`:
  ```python
  from core import db

  def test_agent_devices_has_user_id():
      db.init_db()
      with db._get_conn() as c:
          cols = {r[1] for r in c.execute(
              "PRAGMA table_info('agent_devices')").fetchall()}
      assert "user_id" in cols

  def test_legacy_rows_keep_null_user_id():
      db.init_db()
      with db._get_conn() as c:
          c.execute("INSERT INTO agent_devices (id, name, token_hash, created_at) "
                    "VALUES ('d1', 'D', 'h', '2026-01-01T00:00:00+00:00')")
          c.commit()
          row = c.execute("SELECT user_id FROM agent_devices WHERE id='d1'").fetchone()
      assert row["user_id"] is None
  ```

- [ ] **Step 9.2:** Run — fails.

  ```bash
  pytest tests/test_schema_agent_devices_user_id.py -q
  ```

- [ ] **Step 9.3:** In `core/db.py` extend the existing `agent_devices` idempotent ALTER block:
  ```python
          if "user_id" not in dcols:
              conn.execute("ALTER TABLE agent_devices ADD COLUMN user_id INTEGER")
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_agent_devices_user "
              "ON agent_devices(user_id)"
          )
  ```

- [ ] **Step 9.4:** Pass + commit.

  ```bash
  pytest tests/test_schema_agent_devices_user_id.py -q
  git add core/db.py tests/test_schema_agent_devices_user_id.py
  git commit -m "schema(α): agent_devices.user_id (nullable, idempotent ALTER)"
  ```

---

### Task 10: ALTER secrets ADD COLUMN org_id

- [ ] **Step 10.1:** Failing test.

  Create `tests/test_schema_secrets_org_id.py`:
  ```python
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
  ```

- [ ] **Step 10.2:** Run — fails.

  ```bash
  pytest tests/test_schema_secrets_org_id.py -q
  ```

- [ ] **Step 10.3:** In `core/db.py`, after the secrets CREATE TABLE add:
  ```python
          scols = {r[1] for r in conn.execute(
              "PRAGMA table_info('secrets')").fetchall()}
          if "org_id" not in scols:
              conn.execute("ALTER TABLE secrets ADD COLUMN org_id INTEGER")
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_secrets_org ON secrets(org_id)"
          )
  ```

- [ ] **Step 10.4:** Pass + commit.

  ```bash
  pytest tests/test_schema_secrets_org_id.py -q
  git add core/db.py tests/test_schema_secrets_org_id.py
  git commit -m "schema(α): secrets.org_id (nullable, idempotent ALTER)"
  ```

---

### Task 11: ALTER upload_history ADD COLUMN org_id, user_id

- [ ] **Step 11.1:** Failing test.

  Create `tests/test_schema_upload_history_tenant.py`:
  ```python
  from core import db

  def test_upload_history_has_tenant_columns():
      db.init_db()
      with db._get_conn() as c:
          cols = {r[1] for r in c.execute(
              "PRAGMA table_info('upload_history')").fetchall()}
      assert "org_id" in cols
      assert "user_id" in cols

  def test_legacy_record_upload_still_works(temp_db):
      db.init_db()
      db.record_upload(
          session_id="s1", iso_date="2026-01-01", platform="YouTube Video",
          title="t", file_path="/tmp/a", success=True,
          url="https://youtube.com/watch?v=abc", scheduled_time="",
          error="",
      )
      with db._get_conn() as c:
          row = c.execute(
              "SELECT org_id, user_id FROM upload_history WHERE session_id='s1'"
          ).fetchone()
      assert row["org_id"] is None
      assert row["user_id"] is None
  ```

- [ ] **Step 11.2:** Run — fails.

  ```bash
  pytest tests/test_schema_upload_history_tenant.py -q
  ```

- [ ] **Step 11.3:** In `core/db.py`, extend the existing `upload_history` idempotent ALTER block:
  ```python
          uhcols = {r[1] for r in conn.execute(
              "PRAGMA table_info('upload_history')").fetchall()}
          if "org_id" not in uhcols:
              conn.execute("ALTER TABLE upload_history ADD COLUMN org_id INTEGER")
          if "user_id" not in uhcols:
              conn.execute("ALTER TABLE upload_history ADD COLUMN user_id INTEGER")
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_upload_history_org "
              "ON upload_history(org_id)"
          )
          conn.execute(
              "CREATE INDEX IF NOT EXISTS idx_upload_history_user "
              "ON upload_history(user_id)"
          )
  ```

- [ ] **Step 11.4:** Pass + commit.

  ```bash
  pytest tests/test_schema_upload_history_tenant.py -q
  git add core/db.py tests/test_schema_upload_history_tenant.py
  git commit -m "schema(α): upload_history.org_id + user_id (nullable, idempotent ALTER)"
  ```

---

### Task 12: core/user_store.py — create + lookup + verify_password (Argon2id)

- [ ] **Step 12.1:** Failing test.

  Create `tests/test_user_store.py`:
  ```python
  import pytest
  from core import user_store

  def test_create_user_then_lookup():
      u = user_store.create_user(
          username="alice", email="alice@example.com",
          password="correct horse battery staple"
      )
      assert u["id"] >= 1
      assert u["username"] == "alice"
      assert u["email"] == "alice@example.com"
      assert u["password_hash"].startswith("$argon2id$")
      assert u["program_owner"] == 0
      assert u["password_changed_at"] is None  # forced-change flag

  def test_create_user_program_owner_flag():
      u = user_store.create_user(
          username="admin", email="admin@x.com",
          password="hunter2hunter2", program_owner=True,
      )
      assert u["program_owner"] == 1

  def test_get_user_by_username_email_id():
      u = user_store.create_user(
          username="bob", email="bob@x.com", password="passpasspass1!"
      )
      assert user_store.get_user_by_username("bob")["id"] == u["id"]
      assert user_store.get_user_by_email("bob@x.com")["id"] == u["id"]
      assert user_store.get_user_by_id(u["id"])["username"] == "bob"
      assert user_store.get_user_by_username("nope") is None

  def test_verify_password_accepts_correct_after_password_change():
      u = user_store.create_user(
          username="carol", email="c@x.com", password="originalpw1234"
      )
      # password_changed_at is NULL → verify_password must REJECT until forced change
      assert user_store.verify_password(u["id"], "originalpw1234") is False
      user_store.update_password(u["id"], "newpass1234567")
      assert user_store.verify_password(u["id"], "newpass1234567") is True
      assert user_store.verify_password(u["id"], "wrong") is False

  def test_verify_password_unknown_user_returns_false():
      assert user_store.verify_password(99999, "anything") is False

  def test_update_last_login_at():
      u = user_store.create_user(username="d", email="d@x.com", password="pw1234567890")
      user_store.update_password(u["id"], "newpw1234567890")
      user_store.update_last_login_at(u["id"])
      fresh = user_store.get_user_by_id(u["id"])
      assert fresh["last_login_at"] is not None
  ```

- [ ] **Step 12.2:** Run — fails (module missing).

  ```bash
  pytest tests/test_user_store.py -q
  ```

- [ ] **Step 12.3:** Create `core/user_store.py`:
  ```python
  """Argon2id-backed user store. Multi-tenant phase α.

  password_changed_at is set to NULL on create. A NULL value forces a
  password change on first login (verify_password returns False until
  update_password() is called). This is the migration semantics: when we
  seed the bootstrap user from INITIAL_ADMIN_PASSWORD, the program-owner
  is forced to set a new password before they can actually log in.
  """
  from __future__ import annotations

  from datetime import datetime, timezone
  from typing import Optional

  from argon2 import PasswordHasher
  from argon2.exceptions import VerifyMismatchError, InvalidHash

  from core import db

  # OWASP-recommended Argon2id parameters (per spec).
  _hasher = PasswordHasher(
      time_cost=2,
      memory_cost=65536,
      parallelism=4,
  )


  def _now() -> str:
      return datetime.now(timezone.utc).isoformat()


  def hash_password(plaintext: str) -> str:
      return _hasher.hash(plaintext)


  def create_user(
      username: str,
      email: str,
      password: str,
      program_owner: bool = False,
  ) -> dict:
      """Insert a new user. Returns the inserted row.

      password_changed_at is set to NULL so verify_password() rejects the
      bootstrap password and forces a real change on first login.
      """
      pw_hash = hash_password(password)
      now = _now()
      with db._get_conn() as c:
          cur = c.execute(
              "INSERT INTO users (username, email, password_hash, "
              "email_2fa_enabled, program_owner, created_at, password_changed_at) "
              "VALUES (?, ?, ?, 0, ?, ?, NULL)",
              (username, email, pw_hash, 1 if program_owner else 0, now),
          )
          c.commit()
          new_id = cur.lastrowid
          row = c.execute("SELECT * FROM users WHERE id=?", (new_id,)).fetchone()
      return dict(row)


  def get_user_by_id(user_id: int) -> Optional[dict]:
      with db._get_conn() as c:
          row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
      return dict(row) if row else None


  def get_user_by_username(username: str) -> Optional[dict]:
      with db._get_conn() as c:
          row = c.execute(
              "SELECT * FROM users WHERE username=?", (username,)
          ).fetchone()
      return dict(row) if row else None


  def get_user_by_email(email: str) -> Optional[dict]:
      with db._get_conn() as c:
          row = c.execute(
              "SELECT * FROM users WHERE email=?", (email,)
          ).fetchone()
      return dict(row) if row else None


  def verify_password(user_id: int, plaintext: str) -> bool:
      """Constant-time Argon2id verification.

      Returns False if:
        - the user does not exist,
        - the stored hash is malformed,
        - the password does not match,
        - password_changed_at IS NULL (forced first-login change pending).
      """
      user = get_user_by_id(user_id)
      if not user:
          return False
      if user["password_changed_at"] is None:
          return False
      try:
          _hasher.verify(user["password_hash"], plaintext)
          return True
      except (VerifyMismatchError, InvalidHash):
          return False


  def update_password(user_id: int, new_plaintext: str) -> None:
      """Set a new password and flip password_changed_at=now()."""
      pw_hash = hash_password(new_plaintext)
      now = _now()
      with db._get_conn() as c:
          c.execute(
              "UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
              (pw_hash, now, user_id),
          )
          c.commit()


  def update_last_login_at(user_id: int) -> None:
      now = _now()
      with db._get_conn() as c:
          c.execute(
              "UPDATE users SET last_login_at=? WHERE id=?", (now, user_id)
          )
          c.commit()
  ```

- [ ] **Step 12.4:** Run; pass.

  ```bash
  pytest tests/test_user_store.py -q
  ```

- [ ] **Step 12.5:** Commit.

  ```bash
  git add core/user_store.py tests/test_user_store.py
  git commit -m "feat(α): core/user_store.py (Argon2id + forced first-login change)"
  ```

---

### Task 13: user_store — verify_password rejects when password_changed_at NULL (already done in Task 12; smoke-test boundary cases)

- [ ] **Step 13.1:** Failing test for the rehash-on-parameter-change path.

  Append to `tests/test_user_store.py`:
  ```python
  def test_update_password_unblocks_verify():
      u = user_store.create_user(
          username="eve", email="e@x.com", password="originalpw1!23"
      )
      # Before update_password, verify returns False even on the right pw.
      assert user_store.verify_password(u["id"], "originalpw1!23") is False
      user_store.update_password(u["id"], "newpw9!876543")
      assert user_store.verify_password(u["id"], "newpw9!876543") is True

  def test_verify_password_rejects_unknown_user_id():
      assert user_store.verify_password(0, "x") is False
      assert user_store.verify_password(-1, "x") is False
      assert user_store.verify_password(123_456, "x") is False

  def test_password_hash_is_not_plaintext():
      u = user_store.create_user(
          username="f", email="f@x.com", password="supersecret123"
      )
      assert "supersecret123" not in u["password_hash"]
  ```

- [ ] **Step 13.2:** Run — should pass (the behavior was implemented in Task 12).

  ```bash
  pytest tests/test_user_store.py -q
  ```

- [ ] **Step 13.3:** Commit.

  ```bash
  git add tests/test_user_store.py
  git commit -m "test(α): user_store boundary cases (unknown user, plaintext-free hash)"
  ```

---

### Task 14: core/org_store.py — create_org + lookups

- [ ] **Step 14.1:** Failing test.

  Create `tests/test_org_store.py`:
  ```python
  import pytest
  from core import org_store, user_store

  def test_create_org_then_lookup():
      u = user_store.create_user(username="o", email="o@x.com", password="pw12345678!")
      org = org_store.create_org(
          name="LCBC Church", slug="lcbc-church", created_by_user_id=u["id"]
      )
      assert org["id"] >= 1
      assert org["name"] == "LCBC Church"
      assert org["slug"] == "lcbc-church"
      assert org["plan"] == "free"
      assert org["require_2fa"] == 0

  def test_get_org_by_slug_and_id():
      u = user_store.create_user(username="o", email="o@x.com", password="pw12345678!")
      org = org_store.create_org(name="A", slug="a", created_by_user_id=u["id"])
      assert org_store.get_org_by_slug("a")["id"] == org["id"]
      assert org_store.get_org_by_id(org["id"])["slug"] == "a"
      assert org_store.get_org_by_slug("nope") is None

  def test_list_orgs():
      u = user_store.create_user(username="o", email="o@x.com", password="pw12345678!")
      org_store.create_org(name="A", slug="a", created_by_user_id=u["id"])
      org_store.create_org(name="B", slug="b", created_by_user_id=u["id"])
      slugs = {o["slug"] for o in org_store.list_orgs()}
      assert {"a", "b"} <= slugs

  def test_duplicate_slug_raises():
      u = user_store.create_user(username="o", email="o@x.com", password="pw12345678!")
      org_store.create_org(name="A", slug="a", created_by_user_id=u["id"])
      with pytest.raises(Exception):
          org_store.create_org(name="A2", slug="a", created_by_user_id=u["id"])
  ```

- [ ] **Step 14.2:** Run — fails (module missing).

  ```bash
  pytest tests/test_org_store.py -q
  ```

- [ ] **Step 14.3:** Create `core/org_store.py` (first half — orgs):
  ```python
  """Organization + membership CRUD. Multi-tenant phase α."""
  from __future__ import annotations

  from datetime import datetime, timezone
  from typing import Optional

  from core import db


  def _now() -> str:
      return datetime.now(timezone.utc).isoformat()


  # ---------- Organizations ----------

  def create_org(
      name: str,
      slug: str,
      created_by_user_id: Optional[int] = None,
      plan: str = "free",
      billing_email: Optional[str] = None,
      require_2fa: bool = False,
  ) -> dict:
      now = _now()
      with db._get_conn() as c:
          cur = c.execute(
              "INSERT INTO organizations (name, slug, plan, billing_email, "
              "require_2fa, created_at, created_by_user_id) "
              "VALUES (?, ?, ?, ?, ?, ?, ?)",
              (name, slug, plan, billing_email,
               1 if require_2fa else 0, now, created_by_user_id),
          )
          c.commit()
          row = c.execute(
              "SELECT * FROM organizations WHERE id=?", (cur.lastrowid,)
          ).fetchone()
      return dict(row)


  def get_org_by_id(org_id: int) -> Optional[dict]:
      with db._get_conn() as c:
          row = c.execute(
              "SELECT * FROM organizations WHERE id=?", (org_id,)
          ).fetchone()
      return dict(row) if row else None


  def get_org_by_slug(slug: str) -> Optional[dict]:
      with db._get_conn() as c:
          row = c.execute(
              "SELECT * FROM organizations WHERE slug=?", (slug,)
          ).fetchone()
      return dict(row) if row else None


  def list_orgs() -> list[dict]:
      with db._get_conn() as c:
          rows = c.execute(
              "SELECT * FROM organizations ORDER BY created_at"
          ).fetchall()
      return [dict(r) for r in rows]
  ```

- [ ] **Step 14.4:** Pass + commit.

  ```bash
  pytest tests/test_org_store.py -q
  git add core/org_store.py tests/test_org_store.py
  git commit -m "feat(α): core/org_store.py orgs CRUD"
  ```

---

### Task 15: org_store — memberships CRUD

- [ ] **Step 15.1:** Failing test.

  Append to `tests/test_org_store.py`:
  ```python
  def test_add_membership_and_get():
      u = user_store.create_user(username="m", email="m@x.com", password="pw12345678!")
      org = org_store.create_org(name="O", slug="o", created_by_user_id=u["id"])
      mem = org_store.add_membership(user_id=u["id"], org_id=org["id"], role="owner")
      assert mem["role"] == "owner"
      assert mem["user_id"] == u["id"]
      assert mem["org_id"] == org["id"]
      got = org_store.get_membership(user_id=u["id"], org_id=org["id"])
      assert got["id"] == mem["id"]

  def test_list_memberships_for_user_and_org():
      u1 = user_store.create_user(username="a", email="a@x.com", password="pw12345678!")
      u2 = user_store.create_user(username="b", email="b@x.com", password="pw12345678!")
      o1 = org_store.create_org(name="O1", slug="o1", created_by_user_id=u1["id"])
      o2 = org_store.create_org(name="O2", slug="o2", created_by_user_id=u1["id"])
      org_store.add_membership(user_id=u1["id"], org_id=o1["id"], role="owner")
      org_store.add_membership(user_id=u1["id"], org_id=o2["id"], role="manager")
      org_store.add_membership(user_id=u2["id"], org_id=o1["id"], role="user")
      user_orgs = {m["org_id"] for m in org_store.list_memberships_for_user(u1["id"])}
      assert user_orgs == {o1["id"], o2["id"]}
      org_members = {m["user_id"] for m in org_store.list_members_of_org(o1["id"])}
      assert org_members == {u1["id"], u2["id"]}

  def test_change_role_and_remove():
      u = user_store.create_user(username="r", email="r@x.com", password="pw12345678!")
      o = org_store.create_org(name="O", slug="o", created_by_user_id=u["id"])
      org_store.add_membership(user_id=u["id"], org_id=o["id"], role="user")
      org_store.change_role(user_id=u["id"], org_id=o["id"], role="manager")
      assert org_store.get_membership(user_id=u["id"], org_id=o["id"])["role"] == "manager"
      org_store.remove_membership(user_id=u["id"], org_id=o["id"])
      assert org_store.get_membership(user_id=u["id"], org_id=o["id"]) is None
  ```

- [ ] **Step 15.2:** Run — fails.

  ```bash
  pytest tests/test_org_store.py -q
  ```

- [ ] **Step 15.3:** Append to `core/org_store.py`:
  ```python
  # ---------- Memberships ----------

  def add_membership(user_id: int, org_id: int, role: str) -> dict:
      if role not in ("owner", "manager", "user"):
          raise ValueError(f"invalid role: {role!r}")
      now = _now()
      with db._get_conn() as c:
          cur = c.execute(
              "INSERT INTO org_memberships (user_id, org_id, role, joined_at) "
              "VALUES (?, ?, ?, ?)",
              (user_id, org_id, role, now),
          )
          c.commit()
          row = c.execute(
              "SELECT * FROM org_memberships WHERE id=?", (cur.lastrowid,)
          ).fetchone()
      return dict(row)


  def get_membership(user_id: int, org_id: int) -> Optional[dict]:
      with db._get_conn() as c:
          row = c.execute(
              "SELECT * FROM org_memberships WHERE user_id=? AND org_id=?",
              (user_id, org_id),
          ).fetchone()
      return dict(row) if row else None


  def list_memberships_for_user(user_id: int) -> list[dict]:
      with db._get_conn() as c:
          rows = c.execute(
              "SELECT m.*, o.name AS org_name, o.slug AS org_slug "
              "FROM org_memberships m "
              "JOIN organizations o ON o.id = m.org_id "
              "WHERE m.user_id=? ORDER BY o.name",
              (user_id,),
          ).fetchall()
      return [dict(r) for r in rows]


  def list_members_of_org(org_id: int) -> list[dict]:
      with db._get_conn() as c:
          rows = c.execute(
              "SELECT m.*, u.username, u.email FROM org_memberships m "
              "JOIN users u ON u.id = m.user_id "
              "WHERE m.org_id=? ORDER BY u.username",
              (org_id,),
          ).fetchall()
      return [dict(r) for r in rows]


  def change_role(user_id: int, org_id: int, role: str) -> None:
      if role not in ("owner", "manager", "user"):
          raise ValueError(f"invalid role: {role!r}")
      with db._get_conn() as c:
          c.execute(
              "UPDATE org_memberships SET role=? WHERE user_id=? AND org_id=?",
              (role, user_id, org_id),
          )
          c.commit()


  def remove_membership(user_id: int, org_id: int) -> None:
      with db._get_conn() as c:
          c.execute(
              "DELETE FROM org_memberships WHERE user_id=? AND org_id=?",
              (user_id, org_id),
          )
          c.commit()
  ```

- [ ] **Step 15.4:** Pass + commit.

  ```bash
  pytest tests/test_org_store.py -q
  git add core/org_store.py tests/test_org_store.py
  git commit -m "feat(α): org_store memberships (add/get/list/change_role/remove)"
  ```

---

### Task 16: core/auth.py — rewrite session shape (user_id + current_org_id) with legacy fallback

- [ ] **Step 16.1:** Failing test.

  Create `tests/test_auth_session_shape.py`:
  ```python
  from flask import Flask, session
  from core import auth

  def _app():
      app = Flask(__name__)
      app.secret_key = "test"
      return app

  def test_is_authenticated_via_user_id():
      app = _app()
      with app.test_request_context():
          assert auth.is_authenticated() is False
          session["user_id"] = 42
          assert auth.is_authenticated() is True

  def test_is_authenticated_legacy_boolean_when_enabled(monkeypatch):
      monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "true")
      app = _app()
      with app.test_request_context():
          session["authenticated"] = True
          assert auth.is_authenticated() is True

  def test_is_authenticated_legacy_boolean_ignored_when_flag_off(monkeypatch):
      monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
      app = _app()
      with app.test_request_context():
          session["authenticated"] = True
          assert auth.is_authenticated() is False

  def test_current_user_id_and_current_org_id():
      app = _app()
      with app.test_request_context():
          assert auth.current_user_id() is None
          assert auth.current_org_id() is None
          session["user_id"] = 7
          session["current_org_id"] = 3
          assert auth.current_user_id() == 7
          assert auth.current_org_id() == 3
  ```

- [ ] **Step 16.2:** Run — fails (helpers missing).

  ```bash
  pytest tests/test_auth_session_shape.py -q
  ```

- [ ] **Step 16.3:** Append to `core/auth.py`:
  ```python
  # ---- Multi-tenant phase α: session-shape helpers ----
  #
  # Sessions are keyed by user_id (and optionally current_org_id). The legacy
  # boolean `authenticated` is honored ONLY when LEGACY_PASSWORD_ENABLED is
  # set — gives ops one release to roll back if Argon2id login breaks.

  from flask import session as _flask_session


  def _legacy_enabled() -> bool:
      return (os.environ.get("LEGACY_PASSWORD_ENABLED", "") or "").lower() in (
          "1", "true", "yes",
      )


  def is_authenticated() -> bool:
      if _flask_session.get("user_id") is not None:
          return True
      if _legacy_enabled() and bool(_flask_session.get("authenticated")):
          return True
      return False


  def current_user_id() -> int | None:
      uid = _flask_session.get("user_id")
      return int(uid) if uid is not None else None


  def current_org_id() -> int | None:
      oid = _flask_session.get("current_org_id")
      return int(oid) if oid is not None else None
  ```

  Also update `blueprints/auth.py`'s `is_authenticated()` to delegate:
  ```python
  def is_authenticated() -> bool:
      return auth.is_authenticated()
  ```

- [ ] **Step 16.4:** Pass + commit.

  ```bash
  pytest tests/test_auth_session_shape.py -q
  git add core/auth.py blueprints/auth.py tests/test_auth_session_shape.py
  git commit -m "feat(α): auth session shape (user_id + current_org_id) with legacy fallback"
  ```

---

### Task 17: blueprints/auth.py — POST /login with username + password

- [ ] **Step 17.1:** Failing test.

  Create `tests/test_login_argon2.py`:
  ```python
  import pytest
  from app import create_app
  from core import user_store, org_store

  @pytest.fixture
  def client(monkeypatch):
      monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
      monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-for-cookies")
      app = create_app()
      app.config["TESTING"] = True
      with app.test_client() as c:
          yield c

  def _seed_user_with_active_password(username, email, pw):
      u = user_store.create_user(username=username, email=email, password="bootstrap1234!")
      user_store.update_password(u["id"], pw)
      return u

  def test_login_with_valid_username_password_redirects(client):
      _seed_user_with_active_password("alice", "alice@x.com", "validpw123456!")
      resp = client.post(
          "/login",
          data={"username": "alice", "password": "validpw123456!"},
          follow_redirects=False,
      )
      assert resp.status_code == 302
      with client.session_transaction() as s:
          assert s.get("user_id") is not None
          assert s.get("authenticated") is None  # legacy flag NOT set

  def test_login_unknown_username_401(client):
      resp = client.post(
          "/login",
          data={"username": "nobody", "password": "x"},
      )
      assert resp.status_code == 401

  def test_login_wrong_password_401(client):
      _seed_user_with_active_password("bob", "bob@x.com", "rightpw1234567!")
      resp = client.post(
          "/login",
          data={"username": "bob", "password": "wrongpw1234567!"},
      )
      assert resp.status_code == 401

  def test_login_sets_current_org_id_to_first_membership(client):
      u = _seed_user_with_active_password("carol", "c@x.com", "pw1234567890!")
      o = org_store.create_org(name="O", slug="o", created_by_user_id=u["id"])
      org_store.add_membership(user_id=u["id"], org_id=o["id"], role="owner")
      client.post(
          "/login",
          data={"username": "carol", "password": "pw1234567890!"},
      )
      with client.session_transaction() as s:
          assert s.get("user_id") == u["id"]
          assert s.get("current_org_id") == o["id"]

  def test_login_with_no_memberships_sets_current_org_id_none(client):
      _seed_user_with_active_password("dave", "d@x.com", "pw1234567890!")
      client.post(
          "/login",
          data={"username": "dave", "password": "pw1234567890!"},
      )
      with client.session_transaction() as s:
          assert s.get("user_id") is not None
          assert s.get("current_org_id") is None

  def test_login_user_with_unchanged_password_rejected(client):
      # NEVER-CHANGED user: verify_password returns False (forces change).
      user_store.create_user(username="eve", email="e@x.com", password="originalpw!1234")
      resp = client.post(
          "/login",
          data={"username": "eve", "password": "originalpw!1234"},
      )
      assert resp.status_code == 401
  ```

- [ ] **Step 17.2:** Run — fails.

  ```bash
  pytest tests/test_login_argon2.py -q
  ```

- [ ] **Step 17.3:** Rewrite `blueprints/auth.py`'s `login_submit`:
  ```python
  @bp.route("/login", methods=["POST"])
  def login_submit():
      ip = _client_ip()
      if auth.is_locked(ip):
          return render_template(
              "login.html",
              error="Too many failed attempts. Try again later.",
              legacy_enabled=_legacy_enabled(),
          ), 429

      # Legacy path: the old shared-password form posts only a `password`
      # field. We keep accepting it for one release behind LEGACY_PASSWORD_ENABLED.
      username = (request.form.get("username") or "").strip()
      password = [REDACTED:API key param]"password", "") or ""

      if not username and _legacy_enabled():
          if auth.verify_password(password):  # old shared-password verify
              auth.clear_failures(ip)
              session["authenticated"] = True
              session.permanent = True
              return redirect(_safe_next(request.args.get("next", "")))
          auth.record_failure(ip)
          return render_template(
              "login.html", error="Incorrect password.",
              legacy_enabled=True,
          ), 401

      # New path: username + password (Argon2id).
      from core import user_store, org_store
      user = user_store.get_user_by_username(username)
      if user is None or not user_store.verify_password(user["id"], password):
          auth.record_failure(ip)
          return render_template(
              "login.html", error="Incorrect username or password.",
              legacy_enabled=_legacy_enabled(),
          ), 401

      auth.clear_failures(ip)
      session.clear()
      session["user_id"] = user["id"]
      mems = org_store.list_memberships_for_user(user["id"])
      session["current_org_id"] = mems[0]["org_id"] if mems else None
      session.permanent = True
      user_store.update_last_login_at(user["id"])
      return redirect(_safe_next(request.args.get("next", "")))


  def _legacy_enabled() -> bool:
      return (os.environ.get("LEGACY_PASSWORD_ENABLED", "") or "").lower() in (
          "1", "true", "yes",
      )
  ```

  Update the GET handler:
  ```python
  @bp.route("/login", methods=["GET"])
  def login():
      if is_authenticated():
          return redirect(url_for("scan.index"))
      return render_template(
          "login.html", error=None, legacy_enabled=_legacy_enabled(),
      )
  ```

- [ ] **Step 17.4:** Pass + commit.

  ```bash
  pytest tests/test_login_argon2.py -q
  git add blueprints/auth.py tests/test_login_argon2.py
  git commit -m "feat(α): POST /login accepts username+password (Argon2id); legacy gated"
  ```

---

### Task 18: templates/login.html — username + password fields

- [ ] **Step 18.1:** Failing test.

  Create `tests/test_login_template.py`:
  ```python
  import pytest
  from app import create_app

  @pytest.fixture
  def client(monkeypatch):
      monkeypatch.setenv("FLASK_SECRET_KEY", "test")
      app = create_app()
      app.config["TESTING"] = True
      with app.test_client() as c:
          yield c

  def test_get_login_renders_username_field(client, monkeypatch):
      monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
      resp = client.get("/login")
      body = resp.data.decode("utf-8")
      assert 'name="username"' in body
      assert 'name="password"' in body

  def test_get_login_legacy_mode_only_password(client, monkeypatch):
      monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "true")
      resp = client.get("/login")
      body = resp.data.decode("utf-8")
      assert 'name="password"' in body
      # Legacy form is single-field — no username input shown.
      assert 'name="username"' not in body
  ```

- [ ] **Step 18.2:** Run — fails.

  ```bash
  pytest tests/test_login_template.py -q
  ```

- [ ] **Step 18.3:** Edit `templates/login.html` — replace the form block with:
  ```html
      <h1>Sign in</h1>
      {% if legacy_enabled %}
      <p class="lede">Enter the shared password to access the uploader.</p>
      <form method="POST" action="{{ url_for('auth.login_submit', next=request.args.get('next', '')) }}">
        {% if error %}
        <div class="error" role="alert">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>
          <span>{{ error }}</span>
        </div>
        {% endif %}
        <label for="password">Password</label>
        <input type="password" id="password" name="password" autofocus autocomplete="current-password" placeholder="••••••••••">
        <button type="submit">Sign in</button>
      </form>
      {% else %}
      <p class="lede">Sign in with your username and password.</p>
      <form method="POST" action="{{ url_for('auth.login_submit', next=request.args.get('next', '')) }}">
        {% if error %}
        <div class="error" role="alert">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>
          <span>{{ error }}</span>
        </div>
        {% endif %}
        <label for="username">Username</label>
        <input type="text" id="username" name="username" autofocus autocomplete="username" placeholder="alice">
        <label for="password" style="margin-top:12px;">Password</label>
        <input type="password" id="password" name="password" autocomplete="current-password" placeholder="••••••••••">
        <button type="submit">Sign in</button>
      </form>
      {% endif %}
  ```

  Also add `input[type="text"]` to the input-styling CSS selector that currently only covers `input[type="password"]`:
  ```css
      input[type="password"],
      input[type="text"] {
        width: 100%; padding: 10px 12px;
        background: var(--panel); color: var(--text);
        border: 1px solid var(--border-strong); border-radius: var(--radius-md);
        font: inherit; font-size: 0.9rem; outline: none;
        transition: border-color .15s ease, box-shadow .15s ease;
      }
      input[type="password"]:focus,
      input[type="text"]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  ```

- [ ] **Step 18.4:** Pass + commit.

  ```bash
  pytest tests/test_login_template.py -q
  git add templates/login.html tests/test_login_template.py
  git commit -m "feat(α): login.html shows username+password by default; legacy single-field gated"
  ```

---

### Task 19: core/email.py — Resend wrapper with no-op fallback

- [ ] **Step 19.1:** Failing test.

  Create `tests/test_email.py`:
  ```python
  import logging
  from core import email

  def test_render_returns_subject_html_text():
      subject, html, text = email.render_template(
          "welcome", username="alice", org_name="LCBC Church",
      )
      assert "alice" in html
      assert "alice" in text
      assert subject  # non-empty

  def test_send_noops_without_api_key(monkeypatch, caplog):
      monkeypatch.delenv("RESEND_API_KEY", raising=False)
      with caplog.at_level(logging.WARNING):
          ok = email.send("welcome", to="a@example.com", username="a", org_name="O")
      assert ok is False
      assert any("RESEND_API_KEY" in r.message for r in caplog.records)

  def test_send_calls_resend_when_key_present(monkeypatch):
      monkeypatch.setenv("RESEND_API_KEY", "re_test_xxx")
      calls = []

      class _FakeEmails:
          @staticmethod
          def send(params):
              calls.append(params)
              return {"id": "fake-id"}

      class _FakeResend:
          api_key = None
          Emails = _FakeEmails

      monkeypatch.setattr(email, "resend", _FakeResend, raising=False)
      ok = email.send("welcome", to="b@example.com", username="b", org_name="O")
      assert ok is True
      assert len(calls) == 1
      assert calls[0]["to"] == ["b@example.com"]
      assert calls[0]["subject"]
      assert "html" in calls[0] and "text" in calls[0]

  def test_render_unknown_template_raises():
      import pytest
      with pytest.raises(email.UnknownTemplateError):
          email.render_template("does_not_exist")
  ```

- [ ] **Step 19.2:** Run — fails (module + templates missing).

  ```bash
  pytest tests/test_email.py -q
  ```

- [ ] **Step 19.3:** Create `templates/email/welcome.html`:
  ```html
  <!DOCTYPE html>
  <html><body style="font-family: -apple-system, sans-serif;">
    <h2>Welcome to {{ org_name }} on Daily Life Distributor</h2>
    <p>Hi {{ username }},</p>
    <p>Your account is ready. Sign in at
       <a href="https://autoalert.pro/login">autoalert.pro</a>.</p>
    <p>— Daily Life Distributor</p>
  </body></html>
  ```

  Create `templates/email/welcome.txt`:
  ```
  Welcome to {{ org_name }} on Daily Life Distributor

  Hi {{ username }},

  Your account is ready. Sign in at https://autoalert.pro/login

  — Daily Life Distributor
  ```

  Create `core/email.py`:
  ```python
  """Resend transactional email wrapper.

  Wired but unused: no live emails are sent in phase α (call sites land in
  PR-β invites and PR-γ recovery). render_template() is exercised by tests
  so we know the templates parse and the variable contract is stable.

  Falls back to a WARNING-logged no-op when RESEND_API_KEY is unset so dev
  runs and the bootstrap migration don't require an API key.
  """
  from __future__ import annotations

  import logging
  import os
  from pathlib import Path
  from typing import Tuple

  from jinja2 import Environment, FileSystemLoader, select_autoescape

  try:
      import resend  # type: ignore
  except ImportError:  # pragma: no cover - resend is a required pin
      resend = None  # noqa: F841

  log = logging.getLogger(__name__)


  class UnknownTemplateError(LookupError):
      """Raised when render_template() is called with a missing template name."""


  _TEMPLATE_DIR = (
      Path(__file__).resolve().parent.parent / "templates" / "email"
  )
  _env = Environment(
      loader=FileSystemLoader(str(_TEMPLATE_DIR)),
      autoescape=select_autoescape(["html"]),
      keep_trailing_newline=True,
  )

  # Subject lines per template (one source of truth so render+send agree).
  _SUBJECTS = {
      "welcome": "Welcome to {org_name} on Daily Life Distributor",
  }

  _FROM_ADDR = os.environ.get(
      "RESEND_FROM_ADDR", "Daily Life Distributor <noreply@autoalert.pro>",
  )


  def render_template(name: str, **vars) -> Tuple[str, str, str]:
      """Return (subject, html, text) for the named template.

      Raises UnknownTemplateError if either the .html or .txt is missing
      or the subject is undeclared.
      """
      if name not in _SUBJECTS:
          raise UnknownTemplateError(f"unknown email template: {name!r}")
      try:
          html = _env.get_template(f"{name}.html").render(**vars)
          text = _env.get_template(f"{name}.txt").render(**vars)
      except Exception as e:
          raise UnknownTemplateError(f"failed to render {name}: {e}") from e
      subject = _SUBJECTS[name].format(**vars)
      return subject, html, text


  def send(name: str, to: str, **vars) -> bool:
      """Send a transactional email via Resend.

      Returns True on success, False on no-op (missing API key) or send
      failure. No-op + WARNING when RESEND_API_KEY is unset so the dev path
      and the bootstrap migration both keep working.
      """
      api_key = os.environ.get("RESEND_API_KEY", "").strip()
      if not api_key:
          log.warning(
              "RESEND_API_KEY not set — skipping email %r to %s (template=%s)",
              name, to, name,
          )
          return False
      if resend is None:
          log.error("resend library not importable; skipping email")
          return False

      try:
          subject, html, text = render_template(name, **vars)
      except UnknownTemplateError:
          log.exception("Email template render failed; skipping send")
          return False

      try:
          resend.api_key = api_key
          resend.Emails.send({
              "from": _FROM_ADDR,
              "to": [to],
              "subject": subject,
              "html": html,
              "text": text,
          })
          return True
      except Exception:
          log.exception("Resend send failed for template=%s to=%s", name, to)
          return False
  ```

- [ ] **Step 19.4:** Pass + commit.

  ```bash
  pytest tests/test_email.py -q
  git add core/email.py templates/email/welcome.html templates/email/welcome.txt tests/test_email.py
  git commit -m "feat(α): core/email.py Resend wrapper + welcome template stub"
  ```

---

### Task 20: core/permissions.py — require_program_owner decorator

- [ ] **Step 20.1:** Failing test.

  Create `tests/test_permissions.py`:
  ```python
  import pytest
  from flask import Flask, jsonify
  from core import permissions, user_store

  def _app():
      app = Flask(__name__)
      app.secret_key = "test"

      @app.route("/secret")
      @permissions.require_program_owner
      def secret():
          return jsonify({"ok": True})

      return app

  def test_require_program_owner_blocks_anonymous():
      app = _app()
      with app.test_client() as c:
          resp = c.get("/secret")
          assert resp.status_code in (302, 403)  # redirect to login OR forbidden

  def test_require_program_owner_blocks_non_owner():
      app = _app()
      u = user_store.create_user(username="u", email="u@x.com", password="pw12345678!")
      user_store.update_password(u["id"], "newpw1234567!")
      with app.test_client() as c:
          with c.session_transaction() as s:
              s["user_id"] = u["id"]
          resp = c.get("/secret")
          assert resp.status_code == 403

  def test_require_program_owner_allows_owner():
      app = _app()
      u = user_store.create_user(
          username="admin", email="a@x.com", password="pw12345678!",
          program_owner=True,
      )
      user_store.update_password(u["id"], "newpw1234567!")
      with app.test_client() as c:
          with c.session_transaction() as s:
              s["user_id"] = u["id"]
          resp = c.get("/secret")
          assert resp.status_code == 200
  ```

- [ ] **Step 20.2:** Run — fails.

  ```bash
  pytest tests/test_permissions.py -q
  ```

- [ ] **Step 20.3:** Create `core/permissions.py`:
  ```python
  """Authorization decorators."""
  from __future__ import annotations

  from functools import wraps
  from flask import abort, redirect, request, url_for

  from core import auth, user_store


  def require_program_owner(view):
      """403 unless the session's user is flagged users.program_owner=TRUE.

      Anonymous callers redirect to login (so a clipped URL is recoverable);
      authenticated non-owners get a hard 403 (no leaking which routes exist).
      """
      @wraps(view)
      def wrapped(*args, **kwargs):
          uid = auth.current_user_id()
          if uid is None:
              return redirect(url_for("auth.login", next=request.path))
          user = user_store.get_user_by_id(uid)
          if not user or not user.get("program_owner"):
              abort(403)
          return view(*args, **kwargs)
      return wrapped
  ```

- [ ] **Step 20.4:** Pass + commit.

  ```bash
  pytest tests/test_permissions.py -q
  git add core/permissions.py tests/test_permissions.py
  git commit -m "feat(α): core/permissions.require_program_owner decorator"
  ```

---

### Task 21: blueprints/admin.py — /admin, /admin/organizations, /admin/users

- [ ] **Step 21.1:** Failing test.

  Create `tests/test_blueprints_admin.py`:
  ```python
  import pytest
  from app import create_app
  from core import user_store, org_store

  @pytest.fixture
  def app(monkeypatch):
      monkeypatch.setenv("FLASK_SECRET_KEY", "test")
      return create_app()

  def _owner_login(client):
      u = user_store.create_user(
          username="root", email="root@x.com", password="pwbootstrap1234",
          program_owner=True,
      )
      user_store.update_password(u["id"], "newadminpw12345")
      with client.session_transaction() as s:
          s["user_id"] = u["id"]
      return u

  def _user_login(client):
      u = user_store.create_user(
          username="joe", email="joe@x.com", password="pwbootstrap1234",
      )
      user_store.update_password(u["id"], "newpw12345678")
      with client.session_transaction() as s:
          s["user_id"] = u["id"]
      return u

  def test_admin_landing_requires_program_owner(app):
      with app.test_client() as c:
          _user_login(c)
          resp = c.get("/admin")
          assert resp.status_code == 403

  def test_admin_landing_ok_for_program_owner(app):
      with app.test_client() as c:
          _owner_login(c)
          resp = c.get("/admin")
          assert resp.status_code == 200
          assert b"Admin" in resp.data or b"admin" in resp.data

  def test_admin_organizations_list(app):
      with app.test_client() as c:
          owner = _owner_login(c)
          org_store.create_org(
              name="LCBC Church", slug="lcbc-church",
              created_by_user_id=owner["id"],
          )
          resp = c.get("/admin/organizations")
          assert resp.status_code == 200
          assert b"LCBC Church" in resp.data

  def test_admin_organizations_create(app):
      with app.test_client() as c:
          _owner_login(c)
          resp = c.post(
              "/admin/organizations",
              data={"name": "Acme Corp", "slug": "acme-corp"},
              follow_redirects=False,
          )
          assert resp.status_code in (302, 303)
          assert org_store.get_org_by_slug("acme-corp") is not None

  def test_admin_users_list_shows_all(app):
      with app.test_client() as c:
          _owner_login(c)
          user_store.create_user(username="x", email="x@x.com", password="pw12345678!")
          resp = c.get("/admin/users")
          assert resp.status_code == 200
          assert b"x" in resp.data
  ```

- [ ] **Step 21.2:** Run — fails.

  ```bash
  pytest tests/test_blueprints_admin.py -q
  ```

- [ ] **Step 21.3:** Create `blueprints/admin.py`:
  ```python
  """Program-owner admin (multi-tenant phase α).

  Routes here are gated by users.program_owner = TRUE. The org-create form
  is bare-bones in α — invite-on-create lands in PR-β. The user-list page
  has a force-password-reset action that writes nothing yet (placeholder;
  email sending wires up in PR-β).
  """
  from __future__ import annotations

  import re

  from flask import (
      Blueprint, redirect, render_template, request, url_for, flash,
  )

  from core import org_store, user_store, auth
  from core.permissions import require_program_owner

  bp = Blueprint("admin", __name__, url_prefix="/admin")


  _SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


  def _slugify(s: str) -> str:
      s = s.strip().lower()
      s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
      return s or "org"


  @bp.route("/", methods=["GET"])
  @bp.route("", methods=["GET"])
  @require_program_owner
  def landing():
      orgs = org_store.list_orgs()
      return render_template(
          "admin/organizations.html",
          orgs=orgs,
          form_error=None,
          landing=True,
      )


  @bp.route("/organizations", methods=["GET"])
  @require_program_owner
  def organizations_list():
      orgs = org_store.list_orgs()
      return render_template(
          "admin/organizations.html",
          orgs=orgs,
          form_error=None,
          landing=False,
      )


  @bp.route("/organizations", methods=["POST"])
  @require_program_owner
  def organizations_create():
      name = (request.form.get("name") or "").strip()
      slug = (request.form.get("slug") or "").strip().lower()
      if not name:
          orgs = org_store.list_orgs()
          return render_template(
              "admin/organizations.html",
              orgs=orgs,
              form_error="Org name is required.",
              landing=False,
          ), 400
      if not slug:
          slug = _slugify(name)
      if not _SLUG_RE.match(slug):
          orgs = org_store.list_orgs()
          return render_template(
              "admin/organizations.html",
              orgs=orgs,
              form_error="Slug must be lowercase letters, digits, and dashes.",
              landing=False,
          ), 400
      if org_store.get_org_by_slug(slug):
          orgs = org_store.list_orgs()
          return render_template(
              "admin/organizations.html",
              orgs=orgs,
              form_error=f"Slug {slug!r} already exists.",
              landing=False,
          ), 400
      org_store.create_org(
          name=name, slug=slug,
          created_by_user_id=auth.current_user_id(),
      )
      return redirect(url_for("admin.organizations_list"))


  @bp.route("/users", methods=["GET"])
  @require_program_owner
  def users_list():
      with __import__("core.db", fromlist=["_get_conn"])._get_conn() as c:
          rows = c.execute(
              "SELECT id, username, email, program_owner, created_at, "
              "last_login_at, password_changed_at "
              "FROM users ORDER BY created_at"
          ).fetchall()
      users = [dict(r) for r in rows]
      return render_template("admin/users.html", users=users, notice=None)


  @bp.route("/users/force_reset", methods=["POST"])
  @require_program_owner
  def users_force_reset():
      """Placeholder: PR-β wires the actual Resend send. For now it just
      flips password_changed_at to NULL so the next login is blocked until
      the user calls /reset-password (also PR-β)."""
      user_id = int(request.form.get("user_id") or 0)
      if not user_id:
          return redirect(url_for("admin.users_list"))
      from core import db as _db
      with _db._get_conn() as c:
          c.execute(
              "UPDATE users SET password_changed_at=NULL WHERE id=?",
              (user_id,),
          )
          c.commit()
      return redirect(url_for("admin.users_list"))
  ```

  Register the blueprint in `app.py` near the other registrations:
  ```python
  from blueprints.admin import bp as admin_bp
  app.register_blueprint(admin_bp)
  ```

- [ ] **Step 21.4:** Pass + commit (after Task 22's templates land — for now the route registration is enough; templates come next).

  Note: this commit only adds the blueprint module. Test pass blocks on Task 22 templates.

  ```bash
  git add blueprints/admin.py app.py
  git commit -m "feat(α): blueprints/admin.py program-owner routes (templates next)"
  ```

---

### Task 22: templates/admin/organizations.html + templates/admin/users.html

- [ ] **Step 22.1:** Create `templates/admin/organizations.html`:
  ```html
  <!DOCTYPE html>
  <html lang="en"><head>
    <meta charset="utf-8">
    <title>Admin · Organizations · Daily Life Distributor</title>
    <style>
      body { font-family: system-ui, sans-serif; max-width: 1000px; margin: 24px auto; padding: 0 16px; color: #222; }
      h1 { font-size: 1.4rem; margin-bottom: 16px; }
      h2 { font-size: 1.1rem; margin: 24px 0 8px; }
      nav a { margin-right: 12px; }
      table { width: 100%; border-collapse: collapse; }
      th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; font-size: 0.92rem; }
      form.create { display: flex; gap: 8px; align-items: center; margin: 12px 0 0; }
      input[type=text] { padding: 6px 10px; border: 1px solid #ccc; border-radius: 6px; font: inherit; }
      button { padding: 7px 14px; background: #215fd6; color: #fff; border: 0; border-radius: 6px; font: inherit; cursor: pointer; }
      .err { background: #fde; color: #900; padding: 8px 12px; border-radius: 6px; margin: 8px 0; }
    </style>
  </head><body>
    <nav>
      <a href="{{ url_for('admin.landing') }}">Admin</a>
      <a href="{{ url_for('admin.organizations_list') }}">Organizations</a>
      <a href="{{ url_for('admin.users_list') }}">Users</a>
    </nav>
    <h1>Admin · Organizations</h1>
    {% if form_error %}<div class="err">{{ form_error }}</div>{% endif %}
    <h2>Create organization</h2>
    <form class="create" method="POST" action="{{ url_for('admin.organizations_create') }}">
      <input type="text" name="name" placeholder="Org name" required>
      <input type="text" name="slug" placeholder="slug (auto from name)">
      <button type="submit">Create</button>
    </form>
    <h2>Existing organizations ({{ orgs|length }})</h2>
    <table>
      <thead><tr><th>Name</th><th>Slug</th><th>Plan</th><th>Created</th><th>Disabled?</th></tr></thead>
      <tbody>
        {% for o in orgs %}
        <tr>
          <td>{{ o.name }}</td>
          <td><code>{{ o.slug }}</code></td>
          <td>{{ o.plan }}</td>
          <td>{{ o.created_at }}</td>
          <td>{{ 'Yes' if o.disabled_at else 'No' }}</td>
        </tr>
        {% else %}
        <tr><td colspan="5" style="color:#888;">No organizations yet.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </body></html>
  ```

- [ ] **Step 22.2:** Create `templates/admin/users.html`:
  ```html
  <!DOCTYPE html>
  <html lang="en"><head>
    <meta charset="utf-8">
    <title>Admin · Users · Daily Life Distributor</title>
    <style>
      body { font-family: system-ui, sans-serif; max-width: 1100px; margin: 24px auto; padding: 0 16px; color: #222; }
      h1 { font-size: 1.4rem; margin-bottom: 16px; }
      nav a { margin-right: 12px; }
      table { width: 100%; border-collapse: collapse; }
      th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; font-size: 0.9rem; }
      .pill { padding: 2px 8px; border-radius: 999px; background: #eef; font-size: 0.78rem; }
      button { padding: 4px 10px; background: #c84; color: #fff; border: 0; border-radius: 4px; font: inherit; cursor: pointer; }
    </style>
  </head><body>
    <nav>
      <a href="{{ url_for('admin.landing') }}">Admin</a>
      <a href="{{ url_for('admin.organizations_list') }}">Organizations</a>
      <a href="{{ url_for('admin.users_list') }}">Users</a>
    </nav>
    <h1>Admin · Users ({{ users|length }})</h1>
    {% if notice %}<div>{{ notice }}</div>{% endif %}
    <table>
      <thead><tr><th>Username</th><th>Email</th><th>Program owner</th><th>Created</th><th>Last login</th><th>Pwd changed</th><th></th></tr></thead>
      <tbody>
        {% for u in users %}
        <tr>
          <td>{{ u.username }}</td>
          <td>{{ u.email }}</td>
          <td>{% if u.program_owner %}<span class="pill">OWNER</span>{% endif %}</td>
          <td>{{ u.created_at }}</td>
          <td>{{ u.last_login_at or '—' }}</td>
          <td>{{ u.password_changed_at or 'NEVER' }}</td>
          <td>
            <form method="POST" action="{{ url_for('admin.users_force_reset') }}" style="display:inline;">
              <input type="hidden" name="user_id" value="{{ u.id }}">
              <button type="submit">Force reset</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </body></html>
  ```

- [ ] **Step 22.3:** Run the admin tests (from Task 21).

  ```bash
  pytest tests/test_blueprints_admin.py -q
  ```

- [ ] **Step 22.4:** Commit.

  ```bash
  git add templates/admin/organizations.html templates/admin/users.html
  git commit -m "feat(α): templates/admin/{organizations,users}.html"
  ```

---

### Task 23: core/migration_bootstrap.py — LCBC Church org + bootstrap user + backfills

- [ ] **Step 23.1:** Failing test.

  Create `tests/test_migration_bootstrap.py`:
  ```python
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
  ```

- [ ] **Step 23.2:** Run — fails.

  ```bash
  pytest tests/test_migration_bootstrap.py -q
  ```

- [ ] **Step 23.3:** Create `core/migration_bootstrap.py`:
  ```python
  """Idempotent migration from single-tenant to multi-tenant.

  On first boot of the multi-tenant code:
      1. Ensure schema is migrated (handled by core.db.init_db()).
      2. Create the LCBC Church org if missing.
      3. Create the bootstrap program-owner user if missing.
      4. Add the bootstrap user as Owner of LCBC Church.
      5. Backfill agent_devices.user_id, secrets.org_id,
         upload_history.{org_id,user_id} for legacy NULL rows.

  Idempotent: re-running is a no-op once each step is satisfied.
  Refuses to run if PROGRAM_OWNER_EMAIL is not set — we will not silently
  create a user with no contact address.
  """
  from __future__ import annotations

  import logging
  import os

  from core import db, org_store, user_store

  log = logging.getLogger(__name__)

  _LCBC_NAME = "LCBC Church"
  _LCBC_SLUG = "lcbc-church"
  _BOOTSTRAP_USERNAME = "admin"


  class MigrationAborted(RuntimeError):
      """Raised when required env vars are missing for the first-boot bootstrap."""


  def _backfill_devices(user_id: int) -> int:
      with db._get_conn() as c:
          cur = c.execute(
              "UPDATE agent_devices SET user_id=? WHERE user_id IS NULL",
              (user_id,),
          )
          c.commit()
          return cur.rowcount


  def _backfill_secrets(org_id: int) -> int:
      with db._get_conn() as c:
          cur = c.execute(
              "UPDATE secrets SET org_id=? WHERE org_id IS NULL",
              (org_id,),
          )
          c.commit()
          return cur.rowcount


  def _backfill_upload_history(org_id: int, user_id: int) -> int:
      with db._get_conn() as c:
          cur = c.execute(
              "UPDATE upload_history SET org_id=?, user_id=? "
              "WHERE org_id IS NULL OR user_id IS NULL",
              (org_id, user_id),
          )
          c.commit()
          return cur.rowcount


  def run_migration() -> None:
      """Apply the multi-tenant bootstrap. Idempotent."""
      # Org first — no FK from org to user (created_by_user_id is nullable
      # and is set after the user exists).
      org = org_store.get_org_by_slug(_LCBC_SLUG)
      if org is None:
          org = org_store.create_org(name=_LCBC_NAME, slug=_LCBC_SLUG)
          log.info("Migration: created %r (id=%d)", _LCBC_NAME, org["id"])
      else:
          log.debug("Migration: %r already exists (id=%d)", _LCBC_NAME, org["id"])

      # Bootstrap user.
      email = (os.environ.get("PROGRAM_OWNER_EMAIL") or "").strip()
      if not email:
          # If the user has already been bootstrapped, we don't need the env
          # var — just skip the user-creation step (idempotent re-run).
          with db._get_conn() as c:
              row = c.execute(
                  "SELECT id FROM users WHERE program_owner=1 LIMIT 1"
              ).fetchone()
          if row is None:
              raise MigrationAborted(
                  "PROGRAM_OWNER_EMAIL is required on first boot to create "
                  "the bootstrap program-owner account. Set it in .env and restart."
              )
          user_id = row["id"]
          log.debug("Migration: program-owner already exists (id=%d)", user_id)
      else:
          existing = user_store.get_user_by_email(email)
          if existing is None:
              seed_pw = (os.environ.get("INITIAL_ADMIN_PASSWORD") or "").strip()
              if not seed_pw:
                  raise MigrationAborted(
                      "INITIAL_ADMIN_PASSWORD is required on first boot."
                  )
              created = user_store.create_user(
                  username=_BOOTSTRAP_USERNAME,
                  email=email,
                  password=seed_pw,
                  program_owner=True,
              )
              user_id = created["id"]
              log.info(
                  "Migration: created bootstrap program-owner %s (id=%d). "
                  "Password change is forced on first login.",
                  _BOOTSTRAP_USERNAME, user_id,
              )
          else:
              user_id = existing["id"]
              log.debug("Migration: bootstrap user already exists (id=%d)", user_id)

      # Ensure the bootstrap user is an Owner of LCBC Church.
      mem = org_store.get_membership(user_id=user_id, org_id=org["id"])
      if mem is None:
          org_store.add_membership(
              user_id=user_id, org_id=org["id"], role="owner"
          )
          log.info(
              "Migration: added bootstrap user (id=%d) as Owner of %r",
              user_id, _LCBC_NAME,
          )

      # Backfill legacy rows.
      d = _backfill_devices(user_id)
      s = _backfill_secrets(org["id"])
      h = _backfill_upload_history(org["id"], user_id)
      log.info(
          "Migration: backfilled %d device rows, %d secret rows, %d history rows.",
          d, s, h,
      )
  ```

- [ ] **Step 23.4:** Pass + commit.

  ```bash
  pytest tests/test_migration_bootstrap.py -q
  git add core/migration_bootstrap.py tests/test_migration_bootstrap.py
  git commit -m "feat(α): core/migration_bootstrap.run_migration (LCBC org + bootstrap user + backfills)"
  ```

---

### Task 24: app.py — call run_migration() at startup

- [ ] **Step 24.1:** Failing test.

  Create `tests/test_app_startup_migration.py`:
  ```python
  import pytest
  from app import create_app
  from core import org_store, user_store

  def test_create_app_runs_migration_when_env_present(monkeypatch):
      monkeypatch.setenv("PROGRAM_OWNER_EMAIL", "boot@example.com")
      monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "bootstrappw12345")
      monkeypatch.setenv("FLASK_SECRET_KEY", "test")
      create_app()
      assert org_store.get_org_by_slug("lcbc-church") is not None
      assert user_store.get_user_by_email("boot@example.com") is not None

  def test_create_app_swallows_migration_abort_when_no_bootstrap(monkeypatch, caplog):
      """Re-running create_app() after a successful migration must not crash even if PROGRAM_OWNER_EMAIL was unset on a later boot."""
      monkeypatch.setenv("PROGRAM_OWNER_EMAIL", "boot2@example.com")
      monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "bootstrappw12345")
      monkeypatch.setenv("FLASK_SECRET_KEY", "test")
      create_app()
      monkeypatch.delenv("PROGRAM_OWNER_EMAIL", raising=False)
      # Second create_app() must not raise — the program-owner row already
      # exists, so MigrationAborted should NOT be raised.
      create_app()
  ```

- [ ] **Step 24.2:** Run — fails.

  ```bash
  pytest tests/test_app_startup_migration.py -q
  ```

- [ ] **Step 24.3:** In `app.py`, after the existing `_db.init_db()` + `_db.backfill_external_ids()` block and after the existing `_auth.bootstrap_from_env()` call, add:
  ```python
      # Multi-tenant phase α: idempotent first-boot migration.
      try:
          from core.migration_bootstrap import run_migration as _run_mt_migration
          _run_mt_migration()
      except Exception:
          logging.getLogger(__name__).exception(
              "Multi-tenant migration_bootstrap.run_migration() failed. "
              "First boot requires PROGRAM_OWNER_EMAIL + INITIAL_ADMIN_PASSWORD."
          )
          # We deliberately do NOT re-raise: a missing env var on later boots
          # (after the bootstrap user already exists) must not block startup.
          # The check inside run_migration() handles the "already bootstrapped"
          # path explicitly — only a true first-boot misconfig logs + continues.
  ```

- [ ] **Step 24.4:** Pass + commit.

  ```bash
  pytest tests/test_app_startup_migration.py -q
  git add app.py tests/test_app_startup_migration.py
  git commit -m "feat(α): app.create_app() runs multi-tenant migration_bootstrap"
  ```

---

### Task 25: Header switch-org dropdown when user has >1 membership

- [ ] **Step 25.1:** Failing test.

  Create `tests/test_org_switch.py`:
  ```python
  import pytest
  from app import create_app
  from core import user_store, org_store

  @pytest.fixture
  def client(monkeypatch):
      monkeypatch.setenv("FLASK_SECRET_KEY", "test")
      app = create_app()
      app.config["TESTING"] = True
      with app.test_client() as c:
          yield c

  def _seeded_user(memberships=1):
      u = user_store.create_user(
          username="sw", email="sw@x.com", password="pwbootstrap1234"
      )
      user_store.update_password(u["id"], "newpw12345678!")
      orgs = []
      for i in range(memberships):
          o = org_store.create_org(
              name=f"Org{i}", slug=f"org-{i}", created_by_user_id=u["id"]
          )
          org_store.add_membership(user_id=u["id"], org_id=o["id"], role="owner")
          orgs.append(o)
      return u, orgs

  def test_switch_org_route_changes_session(client):
      u, orgs = _seeded_user(memberships=2)
      with client.session_transaction() as s:
          s["user_id"] = u["id"]
          s["current_org_id"] = orgs[0]["id"]
      resp = client.post(
          "/account/switch_org",
          data={"org_id": orgs[1]["id"]},
          follow_redirects=False,
      )
      assert resp.status_code in (302, 303)
      with client.session_transaction() as s:
          assert s["current_org_id"] == orgs[1]["id"]

  def test_switch_org_rejects_non_member(client):
      u, orgs = _seeded_user(memberships=1)
      with client.session_transaction() as s:
          s["user_id"] = u["id"]
          s["current_org_id"] = orgs[0]["id"]
      # Create an org the user is NOT a member of.
      other = org_store.create_org(
          name="Other", slug="other", created_by_user_id=u["id"]
      )
      resp = client.post(
          "/account/switch_org",
          data={"org_id": other["id"]},
      )
      assert resp.status_code == 403
      with client.session_transaction() as s:
          assert s["current_org_id"] == orgs[0]["id"]
  ```

- [ ] **Step 25.2:** Run — fails.

  ```bash
  pytest tests/test_org_switch.py -q
  ```

- [ ] **Step 25.3:** Add the route. Append to `blueprints/auth.py`:
  ```python
  @bp.route("/account/switch_org", methods=["POST"])
  def switch_org():
      if not is_authenticated():
          return redirect(url_for("auth.login"))
      try:
          new_org_id = int(request.form.get("org_id") or 0)
      except ValueError:
          new_org_id = 0
      if not new_org_id:
          return redirect(request.referrer or url_for("scan.index"))
      from core import org_store
      uid = auth.current_user_id()
      mem = org_store.get_membership(user_id=uid, org_id=new_org_id)
      if mem is None:
          from flask import abort
          abort(403)
      session["current_org_id"] = new_org_id
      return redirect(request.referrer or url_for("scan.index"))
  ```

  Add a Jinja context processor in `app.py` (after `app = Flask(...)` setup, before the blueprint registrations):
  ```python
      @app.context_processor
      def _inject_membership_context():
          from core import auth as _auth, org_store as _os
          uid = _auth.current_user_id()
          if uid is None:
              return {"current_memberships": [], "current_org_id": None}
          mems = _os.list_memberships_for_user(uid)
          return {
              "current_memberships": mems,
              "current_org_id": _auth.current_org_id(),
          }
  ```

  Add the dropdown snippet to `templates/base.html` (inside an existing header div; locate the brand block and append):
  ```html
  {% if current_memberships and current_memberships|length > 1 %}
  <form method="POST" action="{{ url_for('auth.switch_org') }}" style="display:inline-block;">
    <select name="org_id" onchange="this.form.submit()" aria-label="Switch organization">
      {% for m in current_memberships %}
      <option value="{{ m.org_id }}"{% if m.org_id == current_org_id %} selected{% endif %}>{{ m.org_name }}</option>
      {% endfor %}
    </select>
  </form>
  {% endif %}
  ```

- [ ] **Step 25.4:** Pass + commit.

  ```bash
  pytest tests/test_org_switch.py -q
  git add blueprints/auth.py app.py templates/base.html tests/test_org_switch.py
  git commit -m "feat(α): switch-org dropdown (no-op when len(memberships)==1)"
  ```

---

### Task 26: Self-review, full test pass, lint

- [ ] **Step 26.1:** Run the entire test suite.

  ```bash
  pytest -q
  ```

  Any new failure outside the α scope is a regression — fix in place; do not paper over.

- [ ] **Step 26.2:** Run static checks per project convention.

  ```bash
  python -m pyflakes core/user_store.py core/org_store.py core/email.py core/permissions.py core/migration_bootstrap.py blueprints/admin.py
  python -m compileall -q core blueprints
  ```

- [ ] **Step 26.3:** Verify idempotency at the OS level: delete `state.db`, set env, boot once, boot twice; second boot must not log "created" lines:

  ```bash
  rm -f state.db
  PROGRAM_OWNER_EMAIL=owner@example.com INITIAL_ADMIN_PASSWORD=bootstrappw12345 FLASK_SECRET_KEY=x python -c "from app import create_app; create_app()"
  PROGRAM_OWNER_EMAIL=owner@example.com INITIAL_ADMIN_PASSWORD=bootstrappw12345 FLASK_SECRET_KEY=x python -c "from app import create_app; create_app()"
  ```

  Expect: first boot logs "created 'LCBC Church'" + "created bootstrap program-owner"; second boot is silent on those lines.

- [ ] **Step 26.4:** Draft the PR description in `docs/superpowers/plans/pr-alpha-description.md`:
  ```markdown
  # PR-α: Multi-tenant foundation (schema + users + orgs + auth + admin)

  ## What
  Adds the schema, password auth, and program-owner admin pages that the
  rest of the multi-tenant work builds on. No behavior change for existing
  users until they re-authenticate.

  ## Scope (PR-α only)
  - Schema: organizations, users, org_memberships, invitations,
    recovery_codes, recovery_requests, audit_log, audit_log_archive +
    nullable FK columns on agent_devices, secrets, upload_history.
  - Argon2id password hashing.
  - Username + password login. Legacy shared-password kept behind
    LEGACY_PASSWORD_ENABLED=true for one release.
  - Resend wired (core/email.py) but no live emails sent yet.
  - /admin, /admin/organizations, /admin/users (program-owner only).
  - Idempotent migration: creates "LCBC Church" org, bootstrap user from
    PROGRAM_OWNER_EMAIL + INITIAL_ADMIN_PASSWORD, assigns existing data
    to it.
  - Switch-org dropdown in header (no-op for single-org users).

  ## Out of scope (PR-β/γ/δ)
  - Invites + signup form.
  - Role-based authorization on uploads.
  - TOTP/email 2FA, recovery codes, recovery requests.
  - Audit log writes (table exists, hooks come in PR-γ).
  - Concurrency rework + agent download landing page.

  ## Deploy checklist
  1. Set `PROGRAM_OWNER_EMAIL` in .env.
  2. Keep `INITIAL_ADMIN_PASSWORD` set for the first boot.
  3. Boot once with `LEGACY_PASSWORD_ENABLED=true`, then flip off in
     a follow-up commit once you've confirmed login works.
  4. First login forces a password change (password_changed_at=NULL).

  ## Rollback
  Disable the new admin blueprint and set
  `LEGACY_PASSWORD_ENABLED=true`. The schema additions are
  backward-compatible (all new columns nullable, all new tables unused
  by existing code paths).
  ```

- [ ] **Step 26.5:** Draft the squash-commit message:
  ```text
  feat(multi-tenant): phase α — schema + users + orgs + auth + admin

  - schema: organizations, users, org_memberships, invitations,
    recovery_codes, recovery_requests, audit_log, audit_log_archive;
    nullable agent_devices.user_id, secrets.org_id,
    upload_history.{org_id,user_id}. All idempotent.
  - auth: Argon2id (argon2-cffi) password hashing; verify_password
    refuses NULL password_changed_at (forces first-login change).
    Session shape switches to user_id + current_org_id; legacy
    shared-password kept behind LEGACY_PASSWORD_ENABLED.
  - blueprint: /admin, /admin/organizations (list+create),
    /admin/users (list+force_reset) gated by users.program_owner.
  - email: core/email.py Resend wrapper; no-op + WARNING when
    RESEND_API_KEY unset. welcome.html/.txt template stub.
  - migration: core/migration_bootstrap.run_migration() creates
    LCBC Church org + bootstrap program-owner from
    PROGRAM_OWNER_EMAIL + INITIAL_ADMIN_PASSWORD; backfills
    agent_devices.user_id, secrets.org_id, upload_history fields.
  - header: switch-org dropdown when len(memberships) > 1.
  - tests: ~13 new test files, all green; existing suite unaffected.
  ```

- [ ] **Step 26.6:** Final commit + push.

  ```bash
  git add docs/superpowers/plans/pr-alpha-description.md
  git commit -m "docs(α): PR description + rollback checklist"
  git push -u origin docs/multi-tenant-spec
  ```
