# Multi-Tenant Phase β — Invites + Role Enforcement + Per-Org Credentials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make organizations functional: Owners and Managers can invite people (signed-token emails via Resend), recipients accept invites and create accounts that belong to a specific org with a specific role, every route enforces role-based permissions, credentials and devices are scoped per-org, and the relay routes per-org instead of via a singleton "default" account.

**Architecture:** New `core/invitations.py` (CRUD + token issue/verify) and `core/permissions.py` decorators (`@require_role('owner')`, etc.). Templates expand with `/settings/members` (list + invite + role-change + revoke) and `/invite/accept?token=...` (signup form). `core/secrets_store.py` extends every accessor with an optional `org_id` arg; the agent_dispatch + relay code threads `session["current_org_id"]` through every wire path. `core/email.py` flips from no-op to live Resend HTTP calls.

**Tech Stack:** Python 3.11+, Flask, itsdangerous (signed tokens), resend, Argon2id (already deployed in PR-α), pytest.

**Spec:** `docs/superpowers/specs/2026-05-23-multi-tenant-architecture-design.md`

---

## File Structure

| File | New/Modified | Responsibility |
|------|--------------|----------------|
| `core/invitations.py` | New | Issue/verify signed tokens, CRUD for `invitations`, accept-to-membership logic |
| `core/permissions.py` | New | `@require_role(*roles)` + `@require_program_owner` decorators; membership lookup helpers |
| `core/passwords.py` | New | Argon2id wrappers; password policy (length + pwned-top-10k check) |
| `core/email.py` | Modified | Flip no-op stub to live Resend HTTP call with retry/backoff |
| `core/secrets_store.py` | Modified | Extend every accessor with optional `org_id` param; default = NULL (legacy) |
| `core/agent_dispatch.py` | Modified | `collect_credentials(platforms, org_id)`; `start()` reads `session["current_org_id"]` |
| `core/relay.py` | Modified | Replace `_ACCOUNT="default"` singleton with per-org account keying |
| `core/devices.py` | Modified | `register_device` takes `user_id` and persists it on the row |
| `blueprints/invitations.py` | New | `POST /settings/members/invite`, revoke, `GET/POST /invite/accept` |
| `blueprints/members.py` | New | `GET /settings/members`, role-change + remove handlers |
| `blueprints/agent.py` | Modified | `pair_redeem` requires session + records `user_id`/`org_id` on device |
| `blueprints/media.py` | Modified | `batch_run` passes `session["current_org_id"]` into dispatch |
| `blueprints/upload.py` | Modified | Web upload path uses `session["current_org_id"]` for credentials lookup |
| `app.py` | Modified | Register `blueprints/invitations.py` + `blueprints/members.py` |
| `templates/invite_accept.html` | New | Signup form with username/password + org+role banner |
| `templates/members.html` | New | Member list + invite form + pending invites table |
| `templates/email/invite.html` | New | HTML invite email with agent download links |
| `templates/email/invite.txt` | New | Plain-text invite email |
| `templates/email/welcome.html` | New | HTML welcome email (replaces PR-α stub) |
| `templates/email/welcome.txt` | New | Plain-text welcome email |
| `data/pwned_top_10k.txt` | New | Local newline-delimited list of common compromised passwords |
| `tests/test_invitations.py` | New | Token issue/verify, expiry, revoke, accept flow |
| `tests/test_permissions.py` | New | Role decorator matrix; 403s; program-owner bypass |
| `tests/test_org_scoped_secrets.py` | New | `secrets_store` with `org_id`; isolation between orgs |
| `tests/test_org_scoped_relay.py` | New | Relay routes per-org; cross-org isolation |
| `tests/test_members_routes.py` | New | `/settings/members` happy + denied paths |
| `tests/test_email_resend.py` | New | Live-mode mock; retry-on-transient logic |
| `tests/test_pwned_passwords.py` | New | `is_pwned` matches list; rejects in signup |

---

### Task 1 — Signed invitation tokens (`core/invitations.py` foundation)

- [ ] Step 1.1 — Write failing test `tests/test_invitations.py::test_issue_and_verify_token_roundtrip`:
  ```python
  import os
  import pytest
  from core import invitations

  def test_issue_and_verify_token_roundtrip(tmp_path, monkeypatch):
      monkeypatch.setenv("SECRET_ENC_KEY", "k" * 44 + "=")  # any valid Fernet key shape
      raw = invitations.issue_token(invitation_id=42)
      assert isinstance(raw, str) and len(raw) > 20
      payload = invitations.verify_token(raw)
      assert payload == 42
  ```
- [ ] Step 1.2 — Run `pytest tests/test_invitations.py::test_issue_and_verify_token_roundtrip -x` → see `ImportError: cannot import name 'invitations'`.
- [ ] Step 1.3 — Create `core/invitations.py`:
  ```python
  """Invitation tokens + CRUD. Tokens are signed with itsdangerous using a
  secret derived from SECRET_ENC_KEY (NOT the Fernet key itself)."""
  from __future__ import annotations

  import hashlib
  import os
  import secrets as pysecrets
  from datetime import datetime, timedelta, timezone
  from typing import Optional

  from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

  from core import db

  _SALT = "dld.invitations.v1"
  _MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # 7 days

  def _serializer() -> URLSafeTimedSerializer:
      enc_key = os.environ.get("SECRET_ENC_KEY")
      if not enc_key:
          raise RuntimeError("SECRET_ENC_KEY required to issue invitation tokens")
      # Derive a distinct signing secret: SHA-256(SECRET_ENC_KEY || _SALT).
      derived = hashlib.sha256((enc_key + "|" + _SALT).encode("utf-8")).hexdigest()
      return URLSafeTimedSerializer(secret_key=derived, salt=_SALT)

  def issue_token(invitation_id: int) -> str:
      return _serializer().dumps(int(invitation_id))

  def verify_token(raw: str) -> Optional[int]:
      try:
          payload = _serializer().loads(raw, max_age=_MAX_AGE_SECONDS)
      except (BadSignature, SignatureExpired):
          return None
      try:
          return int(payload)
      except (TypeError, ValueError):
          return None
  ```
- [ ] Step 1.4 — Run `pytest tests/test_invitations.py::test_issue_and_verify_token_roundtrip -x` → pass.
- [ ] Step 1.5 — Add `test_verify_token_rejects_tampered`:
  ```python
  def test_verify_token_rejects_tampered(monkeypatch):
      monkeypatch.setenv("SECRET_ENC_KEY", "k" * 44 + "=")
      raw = invitations.issue_token(7)
      tampered = raw[:-2] + ("AA" if raw[-2:] != "AA" else "BB")
      assert invitations.verify_token(tampered) is None
      assert invitations.verify_token("not-a-token") is None
  ```
- [ ] Step 1.6 — Run → pass. Commit: `feat(invitations): signed-token issue/verify keyed by SECRET_ENC_KEY-derived secret`.

---

### Task 2 — Create / revoke / accept invitations (DB layer)

- [ ] Step 2.1 — Add failing `test_create_and_list_pending`:
  ```python
  def test_create_and_list_pending(monkeypatch, app_db):  # app_db = conftest fixture providing a fresh state.db
      monkeypatch.setenv("SECRET_ENC_KEY", "k" * 44 + "=")
      inv_id, token = invitations.create_invitation(
          org_id=1, inviter_user_id=1, email="a@b.com", role="user"
      )
      assert isinstance(inv_id, int)
      assert invitations.verify_token(token) == inv_id
      rows = invitations.list_pending_invitations(org_id=1)
      assert len(rows) == 1 and rows[0]["email"] == "a@b.com" and rows[0]["role"] == "user"
  ```
- [ ] Step 2.2 — Run → fails (`create_invitation` missing).
- [ ] Step 2.3 — Append to `core/invitations.py`:
  ```python
  def _token_hash(raw: str) -> str:
      return hashlib.sha256(raw.encode("utf-8")).hexdigest()

  def create_invitation(
      org_id: int,
      inviter_user_id: int,
      email: str,
      role: str,
      ttl_days: int = 7,
  ) -> tuple[int, str]:
      if role not in ("owner", "manager", "user"):
          raise ValueError(f"invalid role: {role}")
      now = datetime.now(timezone.utc)
      expires = now + timedelta(days=ttl_days)
      with db.connect() as conn:
          cur = conn.execute(
              """INSERT INTO invitations
                 (org_id, inviter_user_id, email, role, token_hash, expires_at, created_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (org_id, inviter_user_id, email.strip().lower(), role,
               "pending-token", expires.isoformat(), now.isoformat()),
          )
          inv_id = int(cur.lastrowid)
          raw = issue_token(inv_id)
          conn.execute(
              "UPDATE invitations SET token_hash = ? WHERE id = ?",
              (_token_hash(raw), inv_id),
          )
          conn.commit()
      return inv_id, raw

  def revoke_invitation(invitation_id: int) -> bool:
      now = datetime.now(timezone.utc).isoformat()
      with db.connect() as conn:
          cur = conn.execute(
              "UPDATE invitations SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL AND accepted_at IS NULL",
              (now, invitation_id),
          )
          conn.commit()
          return cur.rowcount > 0

  def accept_invitation(invitation_id: int, user_id: int) -> bool:
      now = datetime.now(timezone.utc).isoformat()
      with db.connect() as conn:
          row = conn.execute(
              "SELECT org_id, role, accepted_at, revoked_at, expires_at FROM invitations WHERE id = ?",
              (invitation_id,),
          ).fetchone()
          if row is None or row["accepted_at"] or row["revoked_at"]:
              return False
          if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
              return False
          conn.execute(
              """INSERT OR IGNORE INTO org_memberships (user_id, org_id, role, joined_at)
                 VALUES (?, ?, ?, ?)""",
              (user_id, int(row["org_id"]), row["role"], now),
          )
          conn.execute(
              "UPDATE invitations SET accepted_at = ? WHERE id = ?",
              (now, invitation_id),
          )
          conn.commit()
      return True

  def list_pending_invitations(org_id: int) -> list[dict]:
      with db.connect() as conn:
          rows = conn.execute(
              """SELECT id, email, role, created_at, expires_at
                 FROM invitations
                 WHERE org_id = ? AND accepted_at IS NULL AND revoked_at IS NULL
                 ORDER BY created_at DESC""",
              (org_id,),
          ).fetchall()
      return [dict(r) for r in rows]
  ```
- [ ] Step 2.4 — Run → pass.
- [ ] Step 2.5 — Add tests for revoke + accept + expiry:
  ```python
  def test_revoke_then_accept_fails(monkeypatch, app_db):
      monkeypatch.setenv("SECRET_ENC_KEY", "k" * 44 + "=")
      inv_id, _ = invitations.create_invitation(1, 1, "x@y.com", "user")
      assert invitations.revoke_invitation(inv_id) is True
      assert invitations.accept_invitation(inv_id, user_id=99) is False
  ```
- [ ] Step 2.6 — Run → pass. Commit: `feat(invitations): create/revoke/accept + list_pending`.

---

### Task 3 — `list_invitations_by_email` (per-email spam guard)

- [ ] Step 3.1 — Failing test:
  ```python
  def test_list_invitations_by_email_counts_pending_only(monkeypatch, app_db):
      monkeypatch.setenv("SECRET_ENC_KEY", "k" * 44 + "=")
      i1, _ = invitations.create_invitation(1, 1, "spam@x.com", "user")
      i2, _ = invitations.create_invitation(1, 1, "spam@x.com", "user")
      invitations.revoke_invitation(i1)
      pending = invitations.list_invitations_by_email("spam@x.com", org_id=1, status="pending")
      assert [p["id"] for p in pending] == [i2]
  ```
- [ ] Step 3.2 — Run → fails.
- [ ] Step 3.3 — Add to `core/invitations.py`:
  ```python
  def list_invitations_by_email(email: str, org_id: int, status: str = "pending") -> list[dict]:
      email = email.strip().lower()
      with db.connect() as conn:
          if status == "pending":
              rows = conn.execute(
                  """SELECT id, role, created_at, expires_at FROM invitations
                     WHERE email = ? AND org_id = ? AND accepted_at IS NULL AND revoked_at IS NULL""",
                  (email, org_id),
              ).fetchall()
          else:
              rows = conn.execute(
                  "SELECT id, role, created_at, expires_at, accepted_at, revoked_at FROM invitations WHERE email = ? AND org_id = ?",
                  (email, org_id),
              ).fetchall()
      return [dict(r) for r in rows]
  ```
- [ ] Step 3.4 — Run → pass. Commit: `feat(invitations): list_invitations_by_email`.

---

### Task 4 — `core/permissions.py` — `@require_role` decorator

- [ ] Step 4.1 — Failing test `tests/test_permissions.py`:
  ```python
  import pytest
  from flask import Flask, jsonify
  from core.permissions import require_role

  def make_app(role_in_session):
      app = Flask(__name__)
      app.secret_key = "x"
      @app.route("/protected")
      @require_role("owner", "manager")
      def protected():
          return jsonify(ok=True)
      @app.before_request
      def _seed():
          from flask import session
          session["user_id"] = 1
          session["current_org_id"] = 1
          session["_test_role"] = role_in_session
      return app

  def test_require_role_allows_owner(monkeypatch):
      app = make_app("owner")
      monkeypatch.setattr("core.permissions._lookup_role",
                          lambda user_id, org_id: "owner")
      client = app.test_client()
      r = client.get("/protected")
      assert r.status_code == 200

  def test_require_role_denies_user(monkeypatch):
      app = make_app("user")
      monkeypatch.setattr("core.permissions._lookup_role",
                          lambda user_id, org_id: "user")
      r = app.test_client().get("/protected")
      assert r.status_code == 403
  ```
- [ ] Step 4.2 — Run → `ModuleNotFoundError: core.permissions`.
- [ ] Step 4.3 — Create `core/permissions.py`:
  ```python
  """Role-based access control decorators."""
  from __future__ import annotations

  from functools import wraps
  from typing import Optional
  from flask import session, abort, redirect, url_for

  from core import db

  _VALID_ROLES = ("owner", "manager", "user")

  def _lookup_role(user_id: int, org_id: int) -> Optional[str]:
      with db.connect() as conn:
          row = conn.execute(
              "SELECT role FROM org_memberships WHERE user_id = ? AND org_id = ?",
              (user_id, org_id),
          ).fetchone()
      return row["role"] if row else None

  def _is_program_owner(user_id: int) -> bool:
      with db.connect() as conn:
          row = conn.execute(
              "SELECT program_owner FROM users WHERE id = ?", (user_id,),
          ).fetchone()
      return bool(row and row["program_owner"])

  def require_role(*roles: str):
      for r in roles:
          if r not in _VALID_ROLES:
              raise ValueError(f"unknown role: {r}")

      def decorator(fn):
          @wraps(fn)
          def wrapper(*args, **kwargs):
              user_id = session.get("user_id")
              org_id = session.get("current_org_id")
              if not user_id:
                  return redirect(url_for("auth.login"))
              if _is_program_owner(user_id):
                  return fn(*args, **kwargs)
              if not org_id:
                  abort(403)
              role = _lookup_role(user_id, org_id)
              if role not in roles:
                  abort(403)
              return fn(*args, **kwargs)
          return wrapper
      return decorator

  def require_program_owner(fn):
      @wraps(fn)
      def wrapper(*args, **kwargs):
          uid = session.get("user_id")
          if not uid or not _is_program_owner(uid):
              abort(403)
          return fn(*args, **kwargs)
      return wrapper
  ```
- [ ] Step 4.4 — Run → both tests pass.
- [ ] Step 4.5 — Add `test_program_owner_bypasses_role_check`:
  ```python
  def test_program_owner_bypasses(monkeypatch):
      app = make_app("user")
      monkeypatch.setattr("core.permissions._is_program_owner", lambda uid: True)
      monkeypatch.setattr("core.permissions._lookup_role",
                          lambda u, o: None)
      r = app.test_client().get("/protected")
      assert r.status_code == 200
  ```
- [ ] Step 4.6 — Run → pass. Commit: `feat(permissions): @require_role decorator + program-owner bypass`.

---

### Task 5 — `data/pwned_top_10k.txt` + `core/passwords.py`

- [ ] Step 5.1 — Failing test `tests/test_pwned_passwords.py`:
  ```python
  from core import passwords

  def test_pwned_detects_password():
      assert passwords.is_pwned("password") is True
      assert passwords.is_pwned("123456") is True

  def test_pwned_rejects_unique():
      assert passwords.is_pwned("c0rrect-h0rse-battery-staple-9z") is False

  def test_validate_password_length():
      err = passwords.validate_password("short")
      assert err and "12" in err

  def test_validate_password_pwned():
      err = passwords.validate_password("password1234")  # in top-10k as 'password1234' or similar
      assert err is not None
  ```
- [ ] Step 5.2 — Create `data/pwned_top_10k.txt` with at least these entries (one per line, lowercased; full curated list will be appended):
  ```
  123456
  password
  12345678
  qwerty
  123456789
  12345
  1234
  111111
  1234567
  dragon
  123123
  baseball
  abc123
  football
  monkey
  letmein
  shadow
  master
  666666
  qwertyuiop
  123321
  mustang
  1234567890
  michael
  654321
  pussy
  superman
  1qaz2wsx
  7777777
  fuckyou
  121212
  000000
  qazwsx
  123qwe
  killer
  trustno1
  jordan
  jennifer
  zxcvbnm
  asdfgh
  hunter
  buster
  soccer
  harley
  batman
  andrew
  tigger
  sunshine
  iloveyou
  2000
  charlie
  robert
  thomas
  hockey
  ranger
  daniel
  starwars
  klaster
  112233
  george
  computer
  michelle
  jessica
  pepper
  1111
  zxcvbn
  555555
  11111111
  131313
  freedom
  777777
  pass
  fuck
  maggie
  159753
  aaaaaa
  ginger
  princess
  joshua
  cheese
  amanda
  summer
  love
  ashley
  6969
  nicole
  chelsea
  biteme
  matthew
  access
  yankees
  987654321
  dallas
  austin
  thunder
  taylor
  matrix
  password1
  password1234
  ```
  (In practice append the SecLists rockyou-top-10k curated list — this stub is the minimum viable set; tests should still pass against the keys above.)
- [ ] Step 5.3 — Create `core/passwords.py`:
  ```python
  """Password hashing (Argon2id) and policy validation."""
  from __future__ import annotations

  import os
  from functools import lru_cache
  from pathlib import Path
  from typing import Optional

  from argon2 import PasswordHasher
  from argon2.exceptions import VerifyMismatchError

  _HASHER = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=4)

  MIN_LENGTH = 12

  def hash_password(plain: str) -> str:
      return _HASHER.hash(plain)

  def verify_password(plain: str, hashed: str) -> bool:
      try:
          _HASHER.verify(hashed, plain)
          return True
      except VerifyMismatchError:
          return False

  @lru_cache(maxsize=1)
  def _pwned_set() -> frozenset[str]:
      p = Path(__file__).resolve().parent.parent / "data" / "pwned_top_10k.txt"
      if not p.exists():
          return frozenset()
      with p.open("r", encoding="utf-8", errors="replace") as fh:
          return frozenset(line.strip().lower() for line in fh if line.strip())

  def is_pwned(plain: str) -> bool:
      return plain.strip().lower() in _pwned_set()

  def validate_password(plain: str) -> Optional[str]:
      """Returns an error string if password fails policy, else None."""
      if len(plain) < MIN_LENGTH:
          return f"Password must be at least {MIN_LENGTH} characters."
      if is_pwned(plain):
          return "This password appears in a list of common compromised passwords; pick another."
      return None
  ```
- [ ] Step 5.4 — Run `pytest tests/test_pwned_passwords.py -x` → all pass.
- [ ] Step 5.5 — Commit: `feat(passwords): Argon2id wrappers + pwned-top-10k policy`.

---

### Task 6 — `core/email.py` flips to live Resend HTTP call

- [ ] Step 6.1 — Failing test `tests/test_email_resend.py`:
  ```python
  import responses
  from core import email as email_mod

  @responses.activate
  def test_send_invite_calls_resend_api(monkeypatch):
      monkeypatch.setenv("RESEND_API_KEY", "re_test_123")
      responses.add(
          responses.POST, "https://api.resend.com/emails",
          json={"id": "abc-123"}, status=200,
      )
      ok = email_mod.send(
          template_name="invite",
          to="a@b.com",
          org_name="LCBC", inviter_name="Bob", role="user",
          accept_url="https://x/t", agent_win_url="https://x/w", agent_mac_url="https://x/m",
      )
      assert ok is True
      assert responses.calls[0].request.headers["Authorization"] == "Bearer re_test_123"

  @responses.activate
  def test_send_retries_on_transient(monkeypatch):
      monkeypatch.setenv("RESEND_API_KEY", "re_test_123")
      monkeypatch.setattr("core.email._BACKOFF", [0, 0, 0])
      responses.add(responses.POST, "https://api.resend.com/emails", status=502)
      responses.add(responses.POST, "https://api.resend.com/emails", status=502)
      responses.add(responses.POST, "https://api.resend.com/emails",
                    json={"id": "ok"}, status=200)
      assert email_mod.send("invite", "a@b.com",
                            org_name="x", inviter_name="x", role="user",
                            accept_url="x", agent_win_url="x", agent_mac_url="x") is True
      assert len(responses.calls) == 3

  def test_send_noop_when_key_missing(monkeypatch, caplog):
      monkeypatch.delenv("RESEND_API_KEY", raising=False)
      ok = email_mod.send("invite", "a@b.com",
                          org_name="x", inviter_name="x", role="user",
                          accept_url="x", agent_win_url="x", agent_mac_url="x")
      assert ok is False
      assert any("RESEND_API_KEY" in r.message for r in caplog.records)
  ```
- [ ] Step 6.2 — Run → fails (`send` either missing or stubbed).
- [ ] Step 6.3 — Rewrite `core/email.py`:
  ```python
  """Resend transactional email. No-op when RESEND_API_KEY unset (dev mode)."""
  from __future__ import annotations

  import logging
  import os
  import time
  from pathlib import Path
  from typing import Any

  import requests
  from flask import render_template

  log = logging.getLogger(__name__)

  _API_URL = "https://api.resend.com/emails"
  _FROM = os.environ.get("RESEND_FROM", "noreply@autoalert.pro")
  _SUBJECTS = {
      "invite":  "You've been invited to {org_name} on Daily Life Distributor",
      "welcome": "Welcome to {org_name} on Daily Life Distributor",
  }
  _BACKOFF = [0.5, 1.5, 4.0]  # transient retry delays

  def _render(template_name: str, **vars: Any) -> tuple[str, str]:
      html = render_template(f"email/{template_name}.html", **vars)
      txt = render_template(f"email/{template_name}.txt", **vars)
      return html, txt

  def send(template_name: str, to: str, **template_vars: Any) -> bool:
      api_key = os.environ.get("RESEND_API_KEY")
      if not api_key:
          log.warning("RESEND_API_KEY missing — email %s to %s NOT sent", template_name, to)
          return False
      try:
          html, txt = _render(template_name, **template_vars)
      except Exception:
          log.exception("email template render failed: %s", template_name)
          return False
      subject = _SUBJECTS.get(template_name, "Daily Life Distributor").format(
          **{k: v for k, v in template_vars.items() if isinstance(v, str)}
      )
      payload = {"from": _FROM, "to": [to], "subject": subject,
                 "html": html, "text": txt}
      headers = {"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"}
      last_status = None
      for attempt, delay in enumerate(_BACKOFF):
          if delay:
              time.sleep(delay)
          try:
              resp = requests.post(_API_URL, json=payload, headers=headers, timeout=10)
          except requests.RequestException as e:
              log.warning("resend transient (attempt %d): %s", attempt + 1, e)
              continue
          last_status = resp.status_code
          if 200 <= resp.status_code < 300:
              return True
          if resp.status_code >= 500 or resp.status_code == 429:
              log.warning("resend transient %d (attempt %d)", resp.status_code, attempt + 1)
              continue
          log.error("resend permanent failure %d: %s", resp.status_code, resp.text)
          return False
      log.error("resend exhausted retries (last status=%s)", last_status)
      return False
  ```
- [ ] Step 6.4 — `pip install responses` if not installed; run tests → all three pass.
- [ ] Step 6.5 — Commit: `feat(email): live Resend HTTP call with retry-on-transient`.

---

### Task 7 — Invite + welcome email templates

- [ ] Step 7.1 — Failing test extension in `tests/test_email_resend.py`:
  ```python
  def test_invite_template_renders_with_required_vars(app):
      with app.test_request_context():
          from flask import render_template
          html = render_template("email/invite.html",
                                  org_name="LCBC", inviter_name="Bob", role="user",
                                  accept_url="https://x/a", agent_win_url="https://x/w",
                                  agent_mac_url="https://x/m")
          assert "LCBC" in html and "Bob" in html and "https://x/a" in html
          assert "Windows" in html and "macOS" in html
  ```
- [ ] Step 7.2 — Create `templates/email/invite.html`:
  ```html
  <!doctype html>
  <html><body style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 560px; margin: 0 auto;">
    <h2>You've been invited to {{ org_name }}</h2>
    <p>{{ inviter_name }} invited you to join <strong>{{ org_name }}</strong> on
       Daily Life Distributor as a <strong>{{ role }}</strong>.</p>
    <p><a href="{{ accept_url }}"
          style="display:inline-block; padding:12px 20px; background:#2563eb; color:#fff;
                 text-decoration:none; border-radius:6px;">Accept invitation</a></p>
    <p>This link expires in 7 days.</p>
    <hr>
    <p><strong>After accepting, download the agent for your machine:</strong></p>
    <p>
      <a href="{{ agent_win_url }}">Download for Windows</a> &nbsp;|&nbsp;
      <a href="{{ agent_mac_url }}">Download for macOS</a>
    </p>
    <p style="color:#666; font-size:12px;">If you weren't expecting this invitation, you can ignore this email.</p>
  </body></html>
  ```
- [ ] Step 7.3 — Create `templates/email/invite.txt`:
  ```
  You've been invited to {{ org_name }}

  {{ inviter_name }} invited you to join {{ org_name }} on Daily Life Distributor
  as a {{ role }}.

  Accept the invitation:
  {{ accept_url }}

  This link expires in 7 days.

  After accepting, download the agent:
    Windows: {{ agent_win_url }}
    macOS:   {{ agent_mac_url }}

  If you weren't expecting this invitation, you can ignore this email.
  ```
- [ ] Step 7.4 — Create `templates/email/welcome.html`:
  ```html
  <!doctype html>
  <html><body style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 560px; margin: 0 auto;">
    <h2>Welcome to {{ org_name }}!</h2>
    <p>Your account ({{ username }}) is ready. You're a <strong>{{ role }}</strong>
       in {{ org_name }}.</p>
    <p><a href="{{ dashboard_url }}"
          style="display:inline-block; padding:12px 20px; background:#2563eb; color:#fff;
                 text-decoration:none; border-radius:6px;">Open dashboard</a></p>
    <p>Don't forget to install the agent on the machine you'll upload from:</p>
    <p>
      <a href="{{ agent_win_url }}">Download for Windows</a> &nbsp;|&nbsp;
      <a href="{{ agent_mac_url }}">Download for macOS</a>
    </p>
  </body></html>
  ```
- [ ] Step 7.5 — Create `templates/email/welcome.txt`:
  ```
  Welcome to {{ org_name }}!

  Your account ({{ username }}) is ready. You're a {{ role }} in {{ org_name }}.

  Open the dashboard: {{ dashboard_url }}

  Install the agent:
    Windows: {{ agent_win_url }}
    macOS:   {{ agent_mac_url }}
  ```
- [ ] Step 7.6 — Run → pass. Commit: `feat(email): invite + welcome templates (HTML + text)`.

---

### Task 8 — `POST /settings/members/invite` (send invite)

- [ ] Step 8.1 — Failing test `tests/test_invitations.py::test_post_invite_sends_email`:
  ```python
  def test_post_invite_creates_row_and_sends_email(monkeypatch, client_owner):
      sent = []
      monkeypatch.setattr("core.email.send",
                          lambda template_name, to, **kw: sent.append((template_name, to, kw)) or True)
      r = client_owner.post("/settings/members/invite",
                             data={"email": "new@x.com", "role": "user"})
      assert r.status_code in (200, 302)
      assert sent and sent[0][0] == "invite" and sent[0][1] == "new@x.com"
      assert "accept_url" in sent[0][2] and "token=" in sent[0][2]["accept_url"]
  ```
  (Assumes `tests/conftest.py` will gain a `client_owner` fixture that logs in as an Owner in the test org. Add a minimal version: see Step 8.6.)
- [ ] Step 8.2 — Run → fails (route + fixture missing).
- [ ] Step 8.3 — Create `blueprints/invitations.py`:
  ```python
  from __future__ import annotations

  from flask import Blueprint, request, session, redirect, url_for, flash, render_template, abort, current_app
  from flask_limiter.util import get_remote_address

  from core import invitations, email as email_mod, db, passwords
  from core.permissions import require_role

  bp = Blueprint("invitations", __name__)

  _MAX_PENDING_PER_EMAIL = 3
  _AGENT_WIN_URL = "https://autoalert.pro/download/agent/windows"
  _AGENT_MAC_URL = "https://autoalert.pro/download/agent/macos"

  def _inviter_username(user_id: int) -> str:
      with db.connect() as conn:
          row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
      return row["username"] if row else "Someone"

  def _org_name(org_id: int) -> str:
      with db.connect() as conn:
          row = conn.execute("SELECT name FROM organizations WHERE id = ?", (org_id,)).fetchone()
      return row["name"] if row else "your organization"

  @bp.route("/settings/members/invite", methods=["POST"])
  @require_role("owner", "manager")
  def send_invite():
      email_addr = (request.form.get("email") or "").strip().lower()
      role = (request.form.get("role") or "user").strip().lower()
      org_id = int(session["current_org_id"])
      user_id = int(session["user_id"])

      if not email_addr or "@" not in email_addr:
          flash("Enter a valid email address.", "error")
          return redirect(url_for("members.members_page"))
      if role not in ("owner", "manager", "user"):
          flash("Invalid role.", "error")
          return redirect(url_for("members.members_page"))

      # Manager can only invite Users.
      from core.permissions import _lookup_role
      actor_role = _lookup_role(user_id, org_id)
      if actor_role == "manager" and role != "user":
          flash("Managers can only invite Users.", "error")
          return redirect(url_for("members.members_page"))

      pending = invitations.list_invitations_by_email(email_addr, org_id, status="pending")
      if len(pending) >= _MAX_PENDING_PER_EMAIL:
          flash(f"There are already {len(pending)} pending invites for {email_addr}.", "error")
          return redirect(url_for("members.members_page"))

      inv_id, raw_token = invitations.create_invitation(
          org_id=org_id, inviter_user_id=user_id, email=email_addr, role=role,
      )
      accept_url = url_for("invitations.accept_get", token=raw_token, _external=True)
      email_mod.send(
          "invite", to=email_addr,
          org_name=_org_name(org_id),
          inviter_name=_inviter_username(user_id),
          role=role,
          accept_url=accept_url,
          agent_win_url=_AGENT_WIN_URL,
          agent_mac_url=_AGENT_MAC_URL,
      )
      flash(f"Invitation sent to {email_addr}.", "success")
      return redirect(url_for("members.members_page"))
  ```
- [ ] Step 8.4 — Register the blueprint in `app.py` (inside `create_app()`):
  ```python
  from blueprints.invitations import bp as invitations_bp
  app.register_blueprint(invitations_bp)
  ```
  And attach a flask-limiter decorator on `send_invite` using the existing limiter from PR-44 (in `app.py` add `limiter.limit("5/hour", key_func=lambda: session.get("user_id"), exempt_when=lambda: _is_owner())` — easier path is to register the limit dynamically after blueprint registration; see Step 8.5).
- [ ] Step 8.5 — In `app.py` after registering the blueprint, attach the rate limit:
  ```python
  from core.permissions import _lookup_role
  def _invite_exempt_for_owner():
      uid = session.get("user_id"); oid = session.get("current_org_id")
      if not uid or not oid:
          return False
      return _lookup_role(uid, oid) == "owner"
  limiter.limit("5/hour", key_func=lambda: str(session.get("user_id") or "anon"),
                exempt_when=_invite_exempt_for_owner)(invitations_bp)
  ```
- [ ] Step 8.6 — In `tests/conftest.py`, add the `client_owner` fixture:
  ```python
  @pytest.fixture
  def client_owner(client, app_db):
      """Logs in as an Owner of org_id=1."""
      with app_db.connect() as conn:
          conn.execute(
              "INSERT OR IGNORE INTO organizations (id, name, slug, plan, created_at) VALUES (1, 'Test Org', 'test-org', 'free', datetime('now'))"
          )
          cur = conn.execute(
              "INSERT INTO users (username, email, password_hash, created_at) VALUES ('owner1', 'o@x.com', 'x', datetime('now'))"
          )
          uid = cur.lastrowid
          conn.execute("INSERT INTO org_memberships (user_id, org_id, role, joined_at) VALUES (?, 1, 'owner', datetime('now'))", (uid,))
          conn.commit()
      with client.session_transaction() as sess:
          sess["user_id"] = uid
          sess["current_org_id"] = 1
          sess["authenticated"] = True
      return client
  ```
- [ ] Step 8.7 — Run → pass. Commit: `feat(invitations): POST /settings/members/invite with rate limit + spam guard`.

---

### Task 9 — `POST /settings/members/<invitation_id>/revoke`

- [ ] Step 9.1 — Failing test:
  ```python
  def test_owner_can_revoke_invite(client_owner, monkeypatch):
      monkeypatch.setattr("core.email.send", lambda *a, **k: True)
      r = client_owner.post("/settings/members/invite",
                             data={"email": "r@x.com", "role": "user"})
      assert r.status_code in (200, 302)
      from core import invitations
      [pending] = invitations.list_pending_invitations(1)
      r2 = client_owner.post(f"/settings/members/{pending['id']}/revoke")
      assert r2.status_code in (200, 302)
      assert invitations.list_pending_invitations(1) == []
  ```
- [ ] Step 9.2 — Run → 404 / 405 fail.
- [ ] Step 9.3 — Add to `blueprints/invitations.py`:
  ```python
  @bp.route("/settings/members/<int:invitation_id>/revoke", methods=["POST"])
  @require_role("owner", "manager")
  def revoke(invitation_id: int):
      org_id = int(session["current_org_id"])
      user_id = int(session["user_id"])
      with db.connect() as conn:
          row = conn.execute(
              "SELECT org_id, inviter_user_id, role FROM invitations WHERE id = ?",
              (invitation_id,),
          ).fetchone()
      if not row or int(row["org_id"]) != org_id:
          abort(404)
      from core.permissions import _lookup_role
      actor_role = _lookup_role(user_id, org_id)
      # Manager can only revoke invites they created OR User-role invites.
      if actor_role == "manager" and row["inviter_user_id"] != user_id and row["role"] != "user":
          abort(403)
      invitations.revoke_invitation(invitation_id)
      flash("Invitation revoked.", "success")
      return redirect(url_for("members.members_page"))
  ```
- [ ] Step 9.4 — Run → pass.
- [ ] Step 9.5 — Add denied-path test:
  ```python
  def test_manager_cannot_revoke_owner_invite(client_owner, client_manager, monkeypatch):
      monkeypatch.setattr("core.email.send", lambda *a, **k: True)
      client_owner.post("/settings/members/invite", data={"email": "mgr@x.com", "role": "manager"})
      from core import invitations
      [pending] = invitations.list_pending_invitations(1)
      r = client_manager.post(f"/settings/members/{pending['id']}/revoke")
      assert r.status_code == 403
  ```
  (Add `client_manager` fixture analogous to `client_owner` with role="manager".)
- [ ] Step 9.6 — Run → pass. Commit: `feat(invitations): POST /settings/members/<id>/revoke with role rules`.

---

### Task 10 — `GET /invite/accept?token=...` (validate + render signup)

- [ ] Step 10.1 — Failing test:
  ```python
  def test_get_invite_accept_renders_signup(client, monkeypatch, app_db):
      monkeypatch.setenv("SECRET_ENC_KEY", "k" * 44 + "=")
      from core import invitations
      with app_db.connect() as conn:
          conn.execute("INSERT INTO organizations (id, name, slug, plan, created_at) VALUES (1, 'LCBC', 'lcbc', 'free', datetime('now'))")
          conn.execute("INSERT INTO users (id, username, email, password_hash, created_at) VALUES (1, 'a', 'a@x', 'x', datetime('now'))")
          conn.commit()
      _, token = invitations.create_invitation(1, 1, "new@x.com", "manager")
      r = client.get(f"/invite/accept?token={token}")
      assert r.status_code == 200
      assert b"LCBC" in r.data and b"manager" in r.data
      assert b"<form" in r.data and b'name="password"' in r.data

  def test_get_invite_accept_invalid_token_404s(client):
      r = client.get("/invite/accept?token=garbage")
      assert r.status_code == 400
  ```
- [ ] Step 10.2 — Run → fails.
- [ ] Step 10.3 — Add to `blueprints/invitations.py`:
  ```python
  def _load_invitation_for_token(raw_token: str):
      inv_id = invitations.verify_token(raw_token)
      if inv_id is None:
          return None, "Invalid or expired token."
      with db.connect() as conn:
          row = conn.execute(
              """SELECT i.*, o.name AS org_name
                 FROM invitations i
                 JOIN organizations o ON o.id = i.org_id
                 WHERE i.id = ?""",
              (inv_id,),
          ).fetchone()
      if not row:
          return None, "Invitation not found."
      if row["accepted_at"]:
          return None, "This invitation has already been accepted."
      if row["revoked_at"]:
          return None, "This invitation has been revoked."
      return dict(row), None

  @bp.route("/invite/accept", methods=["GET"])
  def accept_get():
      token = request.args.get("token", "")
      inv, err = _load_invitation_for_token(token)
      if err:
          return render_template("invite_accept.html", error=err, invitation=None), 400
      return render_template("invite_accept.html", error=None, invitation=inv, token=token)
  ```
- [ ] Step 10.4 — Create `templates/invite_accept.html` (covers POST as well — see Task 11):
  ```html
  {% extends "base.html" if false else "_blank.html" %}
  <!doctype html>
  <html><head><title>Accept invitation</title></head><body style="font-family: sans-serif; max-width:480px; margin: 40px auto;">
  {% if error %}
    <h2>Invitation problem</h2>
    <p style="color:#a00;">{{ error }}</p>
    <p><a href="{{ url_for('auth.login') }}">Go to login</a></p>
  {% else %}
    <h2>Accept invitation to {{ invitation.org_name }}</h2>
    <p>You're being added as a <strong>{{ invitation.role }}</strong>.</p>
    {% if form_error %}<p style="color:#a00;">{{ form_error }}</p>{% endif %}
    <form method="post" action="{{ url_for('invitations.accept_post') }}">
      <input type="hidden" name="token" value="{{ token }}">
      <label>Username<br><input name="username" required minlength="3" maxlength="32" pattern="[A-Za-z0-9_\-]+"></label><br><br>
      <label>Password (12+ chars, not commonly compromised)<br>
             <input name="password" type="password" required minlength="12"></label><br><br>
      <button type="submit">Create account</button>
    </form>
  {% endif %}
  </body></html>
  ```
  (If `_blank.html` doesn't exist, the `{% extends %}` line can be deleted — the inline html stands alone.)
- [ ] Step 10.5 — Run → both tests pass. Commit: `feat(invitations): GET /invite/accept renders signup`.

---

### Task 11 — `POST /invite/accept` (create user + membership + login)

- [ ] Step 11.1 — Failing test:
  ```python
  def test_post_invite_accept_creates_user_and_logs_in(client, monkeypatch, app_db):
      monkeypatch.setenv("SECRET_ENC_KEY", "k" * 44 + "=")
      monkeypatch.setattr("core.email.send", lambda *a, **k: True)
      from core import invitations
      with app_db.connect() as conn:
          conn.execute("INSERT INTO organizations (id, name, slug, plan, created_at) VALUES (1, 'LCBC', 'lcbc', 'free', datetime('now'))")
          conn.execute("INSERT INTO users (id, username, email, password_hash, created_at) VALUES (1, 'a', 'a@x', 'x', datetime('now'))")
          conn.commit()
      _, token = invitations.create_invitation(1, 1, "n@x.com", "user")
      r = client.post("/invite/accept", data={
          "token": token, "username": "newby",
          "password": "longenough-pw-9zZ!",
      })
      assert r.status_code in (200, 302)
      with app_db.connect() as conn:
          u = conn.execute("SELECT id, username FROM users WHERE username = 'newby'").fetchone()
          assert u is not None
          m = conn.execute("SELECT role FROM org_memberships WHERE user_id = ? AND org_id = 1", (u["id"],)).fetchone()
          assert m["role"] == "user"
      with client.session_transaction() as sess:
          assert sess.get("user_id") == u["id"]
          assert sess.get("current_org_id") == 1

  def test_post_invite_rejects_pwned_password(client, monkeypatch, app_db):
      monkeypatch.setenv("SECRET_ENC_KEY", "k" * 44 + "=")
      from core import invitations
      with app_db.connect() as conn:
          conn.execute("INSERT INTO organizations (id, name, slug, plan, created_at) VALUES (1, 'X', 'x', 'free', datetime('now'))")
          conn.execute("INSERT INTO users (id, username, email, password_hash, created_at) VALUES (1, 'a', 'a@x', 'x', datetime('now'))")
          conn.commit()
      _, token = invitations.create_invitation(1, 1, "p@x.com", "user")
      r = client.post("/invite/accept", data={
          "token": token, "username": "pwned", "password": "password1234",
      })
      assert r.status_code == 400
      with app_db.connect() as conn:
          assert conn.execute("SELECT 1 FROM users WHERE username = 'pwned'").fetchone() is None
  ```
- [ ] Step 11.2 — Run → fails.
- [ ] Step 11.3 — Add to `blueprints/invitations.py`:
  ```python
  import re

  _USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")
  _DASHBOARD_URL = "https://autoalert.pro/"

  @bp.route("/invite/accept", methods=["POST"])
  def accept_post():
      token = (request.form.get("token") or "").strip()
      username = (request.form.get("username") or "").strip()
      password = request.form.get("password") or ""
      inv, err = _load_invitation_for_token(token)
      if err:
          return render_template("invite_accept.html", error=err, invitation=None), 400
      if not _USERNAME_RE.match(username):
          return render_template("invite_accept.html", error=None, invitation=inv, token=token,
                                  form_error="Username must be 3-32 chars: A-Z, a-z, 0-9, _ or -."), 400
      pw_err = passwords.validate_password(password)
      if pw_err:
          return render_template("invite_accept.html", error=None, invitation=inv, token=token,
                                  form_error=pw_err), 400
      pw_hash = passwords.hash_password(password)
      with db.connect() as conn:
          existing = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
          if existing:
              return render_template("invite_accept.html", error=None, invitation=inv, token=token,
                                      form_error="Username already taken."), 400
          cur = conn.execute(
              """INSERT INTO users (username, email, password_hash, created_at)
                 VALUES (?, ?, ?, datetime('now'))""",
              (username, inv["email"], pw_hash),
          )
          new_user_id = int(cur.lastrowid)
          conn.commit()
      ok = invitations.accept_invitation(int(inv["id"]), new_user_id)
      if not ok:
          abort(409)
      session["user_id"] = new_user_id
      session["current_org_id"] = int(inv["org_id"])
      session["authenticated"] = True
      email_mod.send(
          "welcome", to=inv["email"],
          org_name=inv["org_name"], username=username, role=inv["role"],
          dashboard_url=_DASHBOARD_URL,
          agent_win_url=_AGENT_WIN_URL, agent_mac_url=_AGENT_MAC_URL,
      )
      return redirect(url_for("dashboard") if "dashboard" in current_app.view_functions else "/")
  ```
- [ ] Step 11.4 — Run → both tests pass. Commit: `feat(invitations): POST /invite/accept creates user + membership + welcome email`.

---

### Task 12 — `GET /settings/members` (member list + invite UI)

- [ ] Step 12.1 — Failing test `tests/test_members_routes.py`:
  ```python
  def test_members_page_renders_for_owner(client_owner, monkeypatch):
      monkeypatch.setattr("core.email.send", lambda *a, **k: True)
      client_owner.post("/settings/members/invite", data={"email": "p@x.com", "role": "user"})
      r = client_owner.get("/settings/members")
      assert r.status_code == 200
      assert b"p@x.com" in r.data
      assert b"owner1" in r.data

  def test_members_page_denies_user_role(client_user):
      r = client_user.get("/settings/members")
      assert r.status_code == 403
  ```
- [ ] Step 12.2 — Run → fails (route missing).
- [ ] Step 12.3 — Create `blueprints/members.py`:
  ```python
  from __future__ import annotations

  from flask import Blueprint, request, session, redirect, url_for, flash, render_template, abort

  from core import db, invitations
  from core.permissions import require_role, _lookup_role

  bp = Blueprint("members", __name__)

  def _list_members(org_id: int) -> list[dict]:
      with db.connect() as conn:
          rows = conn.execute(
              """SELECT u.id, u.username, u.email, m.role, m.joined_at, u.last_login_at
                 FROM org_memberships m
                 JOIN users u ON u.id = m.user_id
                 WHERE m.org_id = ?
                 ORDER BY m.joined_at ASC""",
              (org_id,),
          ).fetchall()
      return [dict(r) for r in rows]

  @bp.route("/settings/members", methods=["GET"])
  @require_role("owner", "manager")
  def members_page():
      org_id = int(session["current_org_id"])
      user_id = int(session["user_id"])
      actor_role = _lookup_role(user_id, org_id)
      return render_template(
          "members.html",
          members=_list_members(org_id),
          pending=invitations.list_pending_invitations(org_id),
          actor_role=actor_role,
      )
  ```
- [ ] Step 12.4 — Create `templates/members.html`:
  ```html
  <!doctype html>
  <html><head><title>Members</title></head><body style="font-family:sans-serif; max-width:760px; margin:30px auto;">
  <h2>Members</h2>
  <table border="1" cellpadding="6" cellspacing="0">
    <tr><th>Username</th><th>Email</th><th>Role</th><th>Joined</th><th>Last login</th><th></th></tr>
    {% for m in members %}
    <tr>
      <td>{{ m.username }}</td>
      <td>{{ m.email }}</td>
      <td>{{ m.role }}</td>
      <td>{{ m.joined_at }}</td>
      <td>{{ m.last_login_at or '—' }}</td>
      <td>
        {% if actor_role == 'owner' %}
        <form method="post" action="{{ url_for('members.change_role', user_id=m.id) }}" style="display:inline;">
          <select name="role">
            <option value="user" {% if m.role=='user' %}selected{% endif %}>user</option>
            <option value="manager" {% if m.role=='manager' %}selected{% endif %}>manager</option>
            <option value="owner" {% if m.role=='owner' %}selected{% endif %}>owner</option>
          </select>
          <button>Update</button>
        </form>
        {% endif %}
        <form method="post" action="{{ url_for('members.remove_member', user_id=m.id) }}" style="display:inline;"
              onsubmit="return confirm('Remove {{ m.username }}?');">
          <button>Remove</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </table>

  <h3>Invite</h3>
  <form method="post" action="{{ url_for('invitations.send_invite') }}">
    <input type="email" name="email" required placeholder="email@example.com">
    <select name="role">
      <option value="user">user</option>
      {% if actor_role == 'owner' %}
        <option value="manager">manager</option>
        <option value="owner">owner</option>
      {% endif %}
    </select>
    <button>Send invite</button>
  </form>

  <h3>Pending invitations</h3>
  <table border="1" cellpadding="6" cellspacing="0">
    <tr><th>Email</th><th>Role</th><th>Created</th><th>Expires</th><th></th></tr>
    {% for p in pending %}
    <tr>
      <td>{{ p.email }}</td><td>{{ p.role }}</td><td>{{ p.created_at }}</td><td>{{ p.expires_at }}</td>
      <td>
        <form method="post" action="{{ url_for('invitations.revoke', invitation_id=p.id) }}">
          <button>Revoke</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </table>
  </body></html>
  ```
- [ ] Step 12.5 — Register the blueprint in `app.py`:
  ```python
  from blueprints.members import bp as members_bp
  app.register_blueprint(members_bp)
  ```
- [ ] Step 12.6 — Run → pass. Commit: `feat(members): /settings/members listing page`.

---

### Task 13 — `POST /settings/members/<user_id>/role` (Owner-only, with sole-Owner guard)

- [ ] Step 13.1 — Failing test:
  ```python
  def test_owner_can_promote_user(client_owner, app_db):
      with app_db.connect() as conn:
          cur = conn.execute("INSERT INTO users (username, email, password_hash, created_at) VALUES ('u1','u1@x','x',datetime('now'))")
          new_uid = cur.lastrowid
          conn.execute("INSERT INTO org_memberships (user_id, org_id, role, joined_at) VALUES (?, 1, 'user', datetime('now'))", (new_uid,))
          conn.commit()
      r = client_owner.post(f"/settings/members/{new_uid}/role", data={"role": "manager"})
      assert r.status_code in (200, 302)
      with app_db.connect() as conn:
          assert conn.execute("SELECT role FROM org_memberships WHERE user_id = ?", (new_uid,)).fetchone()["role"] == "manager"

  def test_sole_owner_cannot_demote_self(client_owner, app_db):
      with client_owner.session_transaction() as sess:
          uid = sess["user_id"]
      r = client_owner.post(f"/settings/members/{uid}/role", data={"role": "manager"})
      assert r.status_code == 400
  ```
- [ ] Step 13.2 — Run → fails.
- [ ] Step 13.3 — Add to `blueprints/members.py`:
  ```python
  def _owner_count(org_id: int) -> int:
      with db.connect() as conn:
          return int(conn.execute(
              "SELECT COUNT(*) AS c FROM org_memberships WHERE org_id = ? AND role = 'owner'",
              (org_id,),
          ).fetchone()["c"])

  @bp.route("/settings/members/<int:user_id>/role", methods=["POST"])
  @require_role("owner")
  def change_role(user_id: int):
      new_role = (request.form.get("role") or "").strip().lower()
      if new_role not in ("owner", "manager", "user"):
          abort(400)
      org_id = int(session["current_org_id"])
      actor_id = int(session["user_id"])
      with db.connect() as conn:
          target = conn.execute(
              "SELECT role FROM org_memberships WHERE user_id = ? AND org_id = ?",
              (user_id, org_id),
          ).fetchone()
      if not target:
          abort(404)
      # Sole-owner guard: can't demote yourself if you're the only Owner.
      if user_id == actor_id and target["role"] == "owner" and new_role != "owner" and _owner_count(org_id) <= 1:
          flash("You're the only Owner — promote someone else first.", "error")
          return ("Sole-owner demotion blocked", 400)
      with db.connect() as conn:
          conn.execute(
              "UPDATE org_memberships SET role = ? WHERE user_id = ? AND org_id = ?",
              (new_role, user_id, org_id),
          )
          conn.commit()
      flash("Role updated.", "success")
      return redirect(url_for("members.members_page"))
  ```
- [ ] Step 13.4 — Run → pass. Commit: `feat(members): POST role-change with sole-Owner guard`.

---

### Task 14 — `POST /settings/members/<user_id>/remove`

- [ ] Step 14.1 — Failing test:
  ```python
  def test_owner_can_remove_user(client_owner, app_db):
      with app_db.connect() as conn:
          cur = conn.execute("INSERT INTO users (username, email, password_hash, created_at) VALUES ('u2','u2@x','x',datetime('now'))")
          new_uid = cur.lastrowid
          conn.execute("INSERT INTO org_memberships (user_id, org_id, role, joined_at) VALUES (?, 1, 'user', datetime('now'))", (new_uid,))
          conn.commit()
      r = client_owner.post(f"/settings/members/{new_uid}/remove")
      assert r.status_code in (200, 302)
      with app_db.connect() as conn:
          assert conn.execute("SELECT 1 FROM org_memberships WHERE user_id = ? AND org_id = 1", (new_uid,)).fetchone() is None

  def test_manager_cannot_remove_manager(client_manager, app_db):
      with app_db.connect() as conn:
          cur = conn.execute("INSERT INTO users (username, email, password_hash, created_at) VALUES ('mgr2','mgr2@x','x',datetime('now'))")
          new_uid = cur.lastrowid
          conn.execute("INSERT INTO org_memberships (user_id, org_id, role, joined_at) VALUES (?, 1, 'manager', datetime('now'))", (new_uid,))
          conn.commit()
      r = client_manager.post(f"/settings/members/{new_uid}/remove")
      assert r.status_code == 403
  ```
- [ ] Step 14.2 — Run → fails.
- [ ] Step 14.3 — Add to `blueprints/members.py`:
  ```python
  @bp.route("/settings/members/<int:user_id>/remove", methods=["POST"])
  @require_role("owner", "manager")
  def remove_member(user_id: int):
      org_id = int(session["current_org_id"])
      actor_id = int(session["user_id"])
      actor_role = _lookup_role(actor_id, org_id)
      with db.connect() as conn:
          target = conn.execute(
              "SELECT role FROM org_memberships WHERE user_id = ? AND org_id = ?",
              (user_id, org_id),
          ).fetchone()
      if not target:
          abort(404)
      # Manager can only remove Users.
      if actor_role == "manager" and target["role"] != "user":
          abort(403)
      # Owner can't remove themselves if sole Owner.
      if user_id == actor_id and target["role"] == "owner" and _owner_count(org_id) <= 1:
          flash("You're the only Owner — promote someone else first.", "error")
          return ("Sole-owner removal blocked", 400)
      with db.connect() as conn:
          conn.execute(
              "DELETE FROM org_memberships WHERE user_id = ? AND org_id = ?",
              (user_id, org_id),
          )
          conn.commit()
      flash("Member removed.", "success")
      return redirect(url_for("members.members_page"))
  ```
- [ ] Step 14.4 — Run → pass. Commit: `feat(members): POST remove member with role-aware rules`.

---

### Task 15 — `core/secrets_store.py` gains `org_id` parameter

- [ ] Step 15.1 — Failing test `tests/test_org_scoped_secrets.py`:
  ```python
  from core import secrets_store

  def test_set_get_secret_scoped_by_org(app_db, monkeypatch):
      monkeypatch.setenv("SECRET_ENC_KEY", _valid_fernet_key())
      with app_db.connect() as conn:
          conn.execute("INSERT INTO organizations (id, name, slug, plan, created_at) VALUES (1, 'A', 'a', 'free', datetime('now'))")
          conn.execute("INSERT INTO organizations (id, name, slug, plan, created_at) VALUES (2, 'B', 'b', 'free', datetime('now'))")
          conn.commit()
      secrets_store.set_secret("yt_token", "tok-A", org_id=1)
      secrets_store.set_secret("yt_token", "tok-B", org_id=2)
      assert secrets_store.get_secret("yt_token", org_id=1) == "tok-A"
      assert secrets_store.get_secret("yt_token", org_id=2) == "tok-B"

  def test_legacy_null_org_unaffected(app_db, monkeypatch):
      monkeypatch.setenv("SECRET_ENC_KEY", _valid_fernet_key())
      secrets_store.set_secret("legacy_key", "legacy-val")  # org_id default None
      assert secrets_store.get_secret("legacy_key") == "legacy-val"
      assert secrets_store.get_secret("legacy_key", org_id=1) is None
  ```
  (`_valid_fernet_key()` is a helper that returns `Fernet.generate_key().decode()`.)
- [ ] Step 15.2 — Run → fails.
- [ ] Step 15.3 — Edit `core/secrets_store.py`. Add `org_id` param to every public accessor; UNIQUE constraint becomes `(key, org_id)` (NULL treated as a distinct scope):
  ```python
  def get_secret(key: str, org_id: int | None = None) -> str | None:
      with db.connect() as conn:
          if org_id is None:
              row = conn.execute(
                  "SELECT value FROM secrets WHERE key = ? AND org_id IS NULL", (key,)
              ).fetchone()
          else:
              row = conn.execute(
                  "SELECT value FROM secrets WHERE key = ? AND org_id = ?", (key, org_id)
              ).fetchone()
      if not row:
          return None
      return crypto.decrypt(row["value"])

  def set_secret(key: str, value: str, org_id: int | None = None) -> None:
      encrypted = crypto.encrypt(value)
      with db.connect() as conn:
          if org_id is None:
              conn.execute(
                  """INSERT INTO secrets (key, org_id, value, updated_at) VALUES (?, NULL, ?, datetime('now'))
                     ON CONFLICT(key, org_id) DO UPDATE SET value = excluded.value, updated_at = datetime('now')""",
                  (key, encrypted),
              )
          else:
              conn.execute(
                  """INSERT INTO secrets (key, org_id, value, updated_at) VALUES (?, ?, ?, datetime('now'))
                     ON CONFLICT(key, org_id) DO UPDATE SET value = excluded.value, updated_at = datetime('now')""",
                  (key, org_id, encrypted),
              )
          conn.commit()

  def get_blob(key: str, org_id: int | None = None) -> bytes | None:
      raw = get_secret(key, org_id=org_id)
      return raw.encode("utf-8") if raw is not None else None

  def set_blob(key: str, value: bytes, org_id: int | None = None) -> None:
      set_secret(key, value.decode("utf-8"), org_id=org_id)

  def delete_secret(key: str, org_id: int | None = None) -> None:
      with db.connect() as conn:
          if org_id is None:
              conn.execute("DELETE FROM secrets WHERE key = ? AND org_id IS NULL", (key,))
          else:
              conn.execute("DELETE FROM secrets WHERE key = ? AND org_id = ?", (key, org_id))
          conn.commit()
  ```
  (Adjust to match the existing function signatures — keep all existing single-arg call sites working by giving `org_id` a default of `None`.)
- [ ] Step 15.4 — Run all existing secrets tests + new ones → pass.
- [ ] Step 15.5 — Commit: `feat(secrets_store): org_id-scoped get/set/delete; NULL = legacy`.

---

### Task 16 — `core/agent_dispatch.py` threads `org_id`

- [ ] Step 16.1 — Failing test `tests/test_agent_dispatch.py::test_collect_credentials_scoped_by_org` (extend existing file):
  ```python
  def test_collect_credentials_pulls_from_org_scope(monkeypatch, app_db):
      monkeypatch.setenv("SECRET_ENC_KEY", _valid_fernet_key())
      from core import secrets_store, agent_dispatch
      secrets_store.set_secret("yt_token", "TOK-1", org_id=1)
      secrets_store.set_secret("yt_token", "TOK-2", org_id=2)
      creds_1 = agent_dispatch.collect_credentials(["YouTube Video"], org_id=1)
      creds_2 = agent_dispatch.collect_credentials(["YouTube Video"], org_id=2)
      assert creds_1.get("yt_token") == "TOK-1"
      assert creds_2.get("yt_token") == "TOK-2"
  ```
- [ ] Step 16.2 — Run → fails.
- [ ] Step 16.3 — Edit `core/agent_dispatch.py`. Change `collect_credentials(platforms_in_use)` to `collect_credentials(platforms_in_use, org_id: int | None = None)`. Every call inside it that hits `secrets_store.get_secret(...)` now passes `org_id=org_id`:
  ```python
  def collect_credentials(platforms_in_use: list[str], org_id: int | None = None) -> dict:
      creds: dict = {}
      if "YouTube Video" in platforms_in_use or "YouTube Shorts" in platforms_in_use:
          creds["yt_token"] = secrets_store.get_secret("yt_token", org_id=org_id)
          creds["yt_client_secrets"] = secrets_store.get_secret("yt_client_secrets", org_id=org_id)
      if "Simplecast" in platforms_in_use:
          creds["simplecast_session"] = secrets_store.get_blob("simplecast_session", org_id=org_id)
      if "Rock" in platforms_in_use or "Rock Email" in platforms_in_use:
          creds["rock_session"] = secrets_store.get_blob("rock_session", org_id=org_id)
      if "Vista Social" in platforms_in_use:
          creds["vista_session"] = secrets_store.get_blob("vista_session", org_id=org_id)
      return creds
  ```
- [ ] Step 16.4 — Update `start()` (and any other entry point that calls `collect_credentials`) to read `session.get("current_org_id")` and pass it through. If `start()` is invoked from a non-request context (background thread), accept `org_id` as a kwarg and pass it explicitly from the route that spawned the thread.
- [ ] Step 16.5 — Update `on_frame` handlers that write secrets back (e.g. `credentials_updated`) to call `secrets_store.set_secret(..., org_id=org_id)`. Thread the `org_id` through the same channel: the frame's job id maps to a job-state dict that records `org_id` at job start.
- [ ] Step 16.6 — Run all agent_dispatch tests + the new one → pass. Commit: `feat(agent_dispatch): thread org_id through collect_credentials + frame handlers`.

---

### Task 17 — `core/relay.py` — per-org accounts

- [ ] Step 17.1 — Failing test `tests/test_org_scoped_relay.py`:
  ```python
  def test_relay_rooms_isolate_per_org(app_db):
      from core import relay
      class FakeWs:
          def __init__(self): self.sent = []
          def send(self, payload): self.sent.append(payload)
          def close(self): pass
      browser_org1 = FakeWs()
      agent_org1 = FakeWs()
      browser_org2 = FakeWs()
      agent_org2 = FakeWs()
      relay.register_browser(org_id=1, ws=browser_org1)
      relay.register_agent(org_id=1, ws=agent_org1, device_id="d1")
      relay.register_browser(org_id=2, ws=browser_org2)
      relay.register_agent(org_id=2, ws=agent_org2, device_id="d2")
      relay.broadcast_to_agent(org_id=1, payload={"hello": "org1"})
      assert agent_org1.sent == [{"hello": "org1"}]
      assert agent_org2.sent == []
  ```
- [ ] Step 17.2 — Run → fails (`org_id` param missing).
- [ ] Step 17.3 — Edit `core/relay.py`. Replace `_ACCOUNT = "default"` and the singleton `Room` with a `dict[int, Room]` keyed by `org_id`:
  ```python
  from collections import defaultdict
  from threading import RLock

  class Room:
      def __init__(self, org_id: int):
          self.org_id = org_id
          self.browsers: list = []
          self.agents: dict[str, object] = {}
          self.lock = RLock()

  _rooms: dict[int, Room] = {}
  _rooms_lock = RLock()

  def _room(org_id: int) -> Room:
      with _rooms_lock:
          if org_id not in _rooms:
              _rooms[org_id] = Room(org_id)
          return _rooms[org_id]

  def register_browser(org_id: int, ws) -> None:
      r = _room(org_id)
      with r.lock:
          r.browsers.append(ws)

  def register_agent(org_id: int, ws, device_id: str) -> None:
      r = _room(org_id)
      with r.lock:
          r.agents[device_id] = ws

  def unregister_browser(org_id: int, ws) -> None:
      r = _room(org_id)
      with r.lock:
          if ws in r.browsers:
              r.browsers.remove(ws)

  def unregister_agent(org_id: int, device_id: str) -> None:
      r = _room(org_id)
      with r.lock:
          r.agents.pop(device_id, None)

  def broadcast_to_agent(org_id: int, payload: dict, device_id: str | None = None) -> None:
      r = _room(org_id)
      with r.lock:
          targets = [r.agents[device_id]] if device_id and device_id in r.agents else list(r.agents.values())
      for ws in targets:
          try:
              ws.send(payload)
          except Exception:
              pass

  def broadcast_to_browsers(org_id: int, payload: dict) -> None:
      r = _room(org_id)
      with r.lock:
          targets = list(r.browsers)
      for ws in targets:
          try:
              ws.send(payload)
          except Exception:
              pass
  ```
  (Existing call sites that took no `org_id` need updates — every wss route handler now must determine `org_id` from the connecting party: for browsers, from `session["current_org_id"]`; for agents, from the device record's `org_id` (membership lookup via `user_id`).)
- [ ] Step 17.4 — Update the agent + browser wss route handlers in `app.py` / `blueprints/agent.py` to pass `org_id` to `register_*` / `unregister_*` / broadcast helpers.
- [ ] Step 17.5 — Update `core/agent_dispatch.start()` and any other callers — replace any reference to `_ACCOUNT="default"` with the runtime `org_id`.
- [ ] Step 17.6 — Run all relay tests + new test → pass. Commit: `feat(relay): per-org room keying; replace singleton _ACCOUNT`.

---

### Task 18 — `core/devices.py` — record `user_id`

- [ ] Step 18.1 — Failing test (extend `tests/test_devices.py`):
  ```python
  def test_register_device_records_user_id(app_db):
      from core import devices
      with app_db.connect() as conn:
          conn.execute("INSERT INTO users (id, username, email, password_hash, created_at) VALUES (7,'u','u@x','x',datetime('now'))")
          conn.execute("INSERT INTO organizations (id, name, slug, plan, created_at) VALUES (1,'X','x','free',datetime('now'))")
          conn.execute("INSERT INTO org_memberships (user_id, org_id, role, joined_at) VALUES (7,1,'user',datetime('now'))")
          conn.commit()
      dev_id = devices.register_device(name="My Mac", pairing_code="X", user_id=7)
      with app_db.connect() as conn:
          row = conn.execute("SELECT user_id FROM agent_devices WHERE id = ?", (dev_id,)).fetchone()
      assert row["user_id"] == 7
  ```
- [ ] Step 18.2 — Run → fails (no `user_id` param).
- [ ] Step 18.3 — Edit `core/devices.py`:
  ```python
  def register_device(name: str, pairing_code: str, user_id: int | None = None, *, org_id: int | None = None) -> int:
      now = datetime.now(timezone.utc).isoformat()
      with db.connect() as conn:
          cur = conn.execute(
              """INSERT INTO agent_devices (name, pairing_code, user_id, created_at, last_seen_at)
                 VALUES (?, ?, ?, ?, NULL)""",
              (name, pairing_code, user_id, now),
          )
          conn.commit()
          return int(cur.lastrowid)
  ```
  (If existing code passes positional args, keep their behavior — `user_id` defaults to None so legacy calls still work.)
- [ ] Step 18.4 — Run → pass. Commit: `feat(devices): register_device records user_id`.

---

### Task 19 — `blueprints/agent.py::pair_redeem` requires session + records user_id

- [ ] Step 19.1 — Failing test `tests/test_agent_pairing_routes.py::test_pair_redeem_requires_session`:
  ```python
  def test_pair_redeem_unauthenticated_returns_401(client):
      r = client.post("/agent/pair/redeem", json={"code": "ABC123"})
      assert r.status_code == 401

  def test_pair_redeem_records_user_and_org(client_user, app_db, monkeypatch):
      # client_user is the fresh "user"-role fixture; current_org_id=1.
      monkeypatch.setattr("blueprints.agent._mint_pairing_code",
                          lambda *a, **k: "TEST123")
      with app_db.connect() as conn:
          conn.execute("INSERT INTO agent_pairing_codes (code, expires_at) VALUES ('TEST123', datetime('now', '+5 minutes'))")
          conn.commit()
      r = client_user.post("/agent/pair/redeem", json={"code": "TEST123", "name": "laptop"})
      assert r.status_code == 200
      with app_db.connect() as conn:
          row = conn.execute("SELECT user_id FROM agent_devices WHERE name = 'laptop'").fetchone()
      with client_user.session_transaction() as sess:
          assert row["user_id"] == sess["user_id"]
  ```
- [ ] Step 19.2 — Run → fails (no session gate, no user_id on insert).
- [ ] Step 19.3 — Edit `blueprints/agent.py::pair_redeem`:
  ```python
  @bp.route("/agent/pair/redeem", methods=["POST"])
  @require_role("owner", "manager", "user")
  def pair_redeem():
      data = request.get_json(force=True, silent=True) or {}
      code = (data.get("code") or "").strip().upper()
      name = (data.get("name") or "Unnamed device").strip()
      # ... existing code lookup / consumption ...
      user_id = int(session["user_id"])
      device_id = devices.register_device(name=name, pairing_code=code, user_id=user_id)
      # ... existing token mint + response ...
      return jsonify(device_id=device_id, token=token)
  ```
  (Wire the unauthenticated path: without `session.user_id`, `@require_role` already redirects browsers to login; for the agent-pair JSON endpoint we want a 401 not a 302 — add an `if request.is_json: return ("", 401)` guard at the top of `require_role`'s "no user_id" branch, OR a small specialized decorator `@require_authenticated_json`.)
- [ ] Step 19.4 — Add `core/permissions.py::require_authenticated_json` and use it on `pair_redeem` instead of `require_role`:
  ```python
  def require_authenticated_json(fn):
      @wraps(fn)
      def wrapper(*args, **kwargs):
          if not session.get("user_id"):
              return ("", 401)
          return fn(*args, **kwargs)
      return wrapper
  ```
  Then `@require_authenticated_json` on `pair_redeem`. (We still want any logged-in member to be able to pair — Users included — so role doesn't matter here.)
- [ ] Step 19.5 — Run → pass. Commit: `feat(agent): pair_redeem requires session and records user_id`.

---

### Task 20 — `blueprints/media.py` + `blueprints/upload.py` thread `org_id`

- [ ] Step 20.1 — Failing test (extend `tests/test_media_batch_run.py`):
  ```python
  def test_batch_run_dispatches_with_current_org_id(client_owner, monkeypatch):
      captured = {}
      def fake_run_batch(*args, **kwargs):
          captured["org_id"] = kwargs.get("org_id")
          return {"status": "ok"}
      monkeypatch.setattr("core.upload_jobs.run_batch", fake_run_batch)
      # ... set up a valid run_id + reassembled batch ...
      r = client_owner.post("/media/batch/run", json={"run_id": "abc", "batch_index": 0})
      assert captured["org_id"] == 1
  ```
- [ ] Step 20.2 — Run → fails (org_id not threaded).
- [ ] Step 20.3 — Edit `blueprints/media.py::batch_run`:
  ```python
  @bp.route("/media/batch/run", methods=["POST"])
  @require_role("owner", "manager", "user")
  def batch_run():
      org_id = int(session["current_org_id"])
      # ... existing payload parsing ...
      result = core.upload_jobs.run_batch(
          entries=entries, summary=summary, job_id=job_id,
          org_id=org_id,  # new
      )
      return jsonify(result)
  ```
- [ ] Step 20.4 — Edit `blueprints/upload.py` (web-path entry point) the same way: read `session["current_org_id"]` and pass into the upload-job entry point.
- [ ] Step 20.5 — Edit `core/upload_jobs.run_batch` signature to accept `org_id: int | None = None`. Pass `org_id` down to `_dispatch_upload`, which forwards to per-platform credential lookups (now using `secrets_store.get_secret(..., org_id=org_id)`).
- [ ] Step 20.6 — Run all media/upload tests + the new one → pass. Commit: `feat(upload): web + agent dispatch use session.current_org_id`.

---

### Task 21 — Apply `@require_role` to every state-changing route

- [ ] Step 21.1 — Audit pass. Run `ctx_search` for `route.*POST` across `blueprints/`:
  ```
  ctx_search '@bp\.route.*methods=\["POST"' --path blueprints/
  ```
- [ ] Step 21.2 — Failing test `tests/test_permissions.py::test_state_changing_routes_require_role`:
  ```python
  PROTECTED_POSTS = [
      ("/settings/members/invite",    {"email":"x@y","role":"user"}),
      ("/settings/members/1/role",     {"role":"manager"}),
      ("/settings/members/1/remove",   {}),
      ("/upload",                       {}),
      ("/media/run/init",               {}),
      ("/media/scan",                   {"categories": {}}),
      ("/admin/organizations",          {"name":"x", "slug":"x", "owner_email":"o@x"}),
  ]
  def test_anonymous_post_redirects_or_denies(client):
      for path, payload in PROTECTED_POSTS:
          r = client.post(path, data=payload, follow_redirects=False)
          assert r.status_code in (302, 401, 403), f"{path} → {r.status_code}"
  ```
- [ ] Step 21.3 — Run → enumerate the failures.
- [ ] Step 21.4 — Add `@require_role(...)` to each failing route in turn. For `/admin/*` use `@require_program_owner`. For owner/manager-only flows use `("owner","manager")`. For run-uploads use `("owner","manager","user")`. Re-run after each edit.
- [ ] Step 21.5 — Run the matrix test → all pass.
- [ ] Step 21.6 — Commit: `feat(permissions): apply @require_role to every state-changing route`.

---

### Task 22 — Tests: org isolation end-to-end

- [ ] Step 22.1 — Add `tests/test_org_scoped_secrets.py::test_org_a_cannot_read_org_b_secrets_via_http`:
  ```python
  def test_member_a_cannot_see_org_b_credentials(client_owner, client_owner_b, monkeypatch):
      from core import secrets_store
      secrets_store.set_secret("yt_token", "TOKEN-A", org_id=1)
      secrets_store.set_secret("yt_token", "TOKEN-B", org_id=2)
      # client_owner is Owner of org 1, client_owner_b is Owner of org 2.
      r_a = client_owner.get("/settings/credentials")
      r_b = client_owner_b.get("/settings/credentials")
      assert b"TOKEN-B" not in r_a.data
      assert b"TOKEN-A" not in r_b.data
  ```
  (Assumes `/settings/credentials` masks the token to last 4 chars — adjust assertion to check the masked form of TOKEN-A vs TOKEN-B.)
- [ ] Step 22.2 — Add `client_owner_b` fixture analogous to `client_owner` but for `org_id=2`.
- [ ] Step 22.3 — Run → confirm isolation. Fix any place that leaks cross-org data.
- [ ] Step 22.4 — Add `tests/test_org_scoped_relay.py::test_agent_broadcast_does_not_leak_across_org`:
  ```python
  def test_cross_org_broadcast_isolation(monkeypatch):
      from core import relay
      class FakeWs:
          def __init__(self): self.sent = []
          def send(self, p): self.sent.append(p)
          def close(self): pass
      a1 = FakeWs(); b1 = FakeWs(); a2 = FakeWs(); b2 = FakeWs()
      relay.register_browser(1, b1); relay.register_agent(1, a1, "d1")
      relay.register_browser(2, b2); relay.register_agent(2, a2, "d2")
      relay.broadcast_to_browsers(1, {"event": "for-org-1"})
      assert b1.sent == [{"event": "for-org-1"}]
      assert b2.sent == []
  ```
- [ ] Step 22.5 — Run → pass.
- [ ] Step 22.6 — Commit: `test(multi-tenant): cross-org isolation for secrets + relay`.

---

### Task 23 — Resend rate-limit decorator wiring for invites

- [ ] Step 23.1 — Failing test `tests/test_rate_limits.py::test_manager_invite_capped_at_5_per_hour`:
  ```python
  def test_manager_capped(client_manager, monkeypatch):
      monkeypatch.setattr("core.email.send", lambda *a, **k: True)
      for i in range(5):
          r = client_manager.post("/settings/members/invite",
                                    data={"email": f"u{i}@x.com", "role": "user"})
          assert r.status_code in (200, 302), f"invite {i} got {r.status_code}"
      r = client_manager.post("/settings/members/invite",
                                data={"email": "u6@x.com", "role": "user"})
      assert r.status_code == 429

  def test_owner_uncapped(client_owner, monkeypatch):
      monkeypatch.setattr("core.email.send", lambda *a, **k: True)
      for i in range(8):
          r = client_owner.post("/settings/members/invite",
                                  data={"email": f"o{i}@x.com", "role": "user"})
          assert r.status_code in (200, 302), f"invite {i} got {r.status_code}"
  ```
- [ ] Step 23.2 — Run → likely fails for one of the two (either both pass through or both get capped).
- [ ] Step 23.3 — Refine the limiter attachment in `app.py`:
  ```python
  @invitations_bp.before_request
  def _rate_limit_invites_for_managers():
      if request.endpoint != "invitations.send_invite":
          return
      uid = session.get("user_id"); oid = session.get("current_org_id")
      if not uid or not oid:
          return
      if _lookup_role(uid, oid) == "owner":
          return  # exempt
      # apply 5/hour limit for non-owners on this endpoint
      limit_check = limiter.limit(
          "5/hour",
          key_func=lambda: f"invite:{uid}",
      )
      # flask_limiter exposes a manual check via the .limit().__call__ pattern;
      # if not available, use limiter.test() with the explicit key:
      if not limiter.test("5/hour", key=f"invite:{uid}"):
          abort(429)
      limiter.hit("5/hour", key=f"invite:{uid}")
  ```
  (Exact API depends on the flask-limiter version deployed in PR-44. If `.test`/`.hit` aren't available, fall back to a manual SQLite-backed counter keyed by `(uid, hour_bucket)`.)
- [ ] Step 23.4 — Run → both tests pass.
- [ ] Step 23.5 — Commit: `feat(invitations): 5/hour cap for Managers; Owners uncapped`.

---

### Task 24 — Self-review

- [ ] Step 24.1 — Run the full test suite: `pytest tests/ -x`. Confirm green.
- [ ] Step 24.2 — Grep for stragglers:
  ```
  ctx_search '_ACCOUNT\s*=\s*"default"' --path .
  ctx_search 'secrets_store\.(get|set)_secret\([^,]*\)' --path . | grep -v 'org_id'
  ctx_search '@bp\.route' --path blueprints/ -C 2 | grep -B1 -A1 'methods=\["POST"' | grep -v 'require_role\|require_program_owner\|require_authenticated_json'
  ```
  Every `_ACCOUNT="default"` reference must be gone. Every `secrets_store` call from app code (not migration code) must pass `org_id`. Every POST route must have a role decorator.
- [ ] Step 24.3 — Manually walk through a fresh-clone flow on a dev DB:
  1. Boot app → log in as program-owner.
  2. Create org "Test Org" + invite founding Owner.
  3. Open the invite-link in a private window → sign up → land on dashboard.
  4. From the new Owner account, invite a Manager.
  5. Accept that invite, log in as Manager, try to invite an Owner → blocked.
  6. As Manager, invite a User; accept.
  7. As Owner, change User → Manager → User. Try to demote yourself as sole Owner → blocked.
  8. Connect a fake YouTube secret as org 1; switch user to a different org; confirm cannot see org 1's token.
- [ ] Step 24.4 — Fix any rough edges discovered; commit each as a separate small commit.

---

### Task 25 — Commit message + PR description

- [ ] Step 25.1 — Final commit-message draft:
  ```
  feat(multi-tenant): PR-β — invite flow, role enforcement, per-org credentials, Resend live

  - Signed-token invitations (itsdangerous, SECRET_ENC_KEY-derived secret).
  - /settings/members: list, invite (POST), role-change (Owner-only), remove (role-aware).
  - /invite/accept: GET renders signup; POST creates user + membership + welcome email.
  - core/permissions.py: @require_role + @require_program_owner; program-owner bypass.
  - core/passwords.py: Argon2id + pwned-top-10k policy (data/pwned_top_10k.txt).
  - core/email.py: live Resend HTTP call with 3-attempt exponential retry.
  - core/secrets_store.py: every accessor gains optional org_id; NULL = legacy.
  - core/agent_dispatch.py: collect_credentials + frame handlers thread org_id.
  - core/relay.py: per-org rooms keyed by org_id; singleton _ACCOUNT="default" removed.
  - core/devices.py + blueprints/agent.py: pair_redeem requires session + records user_id.
  - blueprints/media.py + upload.py: dispatch passes session.current_org_id.
  - Every state-changing route guarded by @require_role / @require_program_owner.
  - Tests: invitations, permissions, org-scoped secrets, org-scoped relay,
    members routes, email resend mock, pwned passwords, rate limits.
  ```
- [ ] Step 25.2 — PR description draft (`/tmp/pr-beta.md`):
  ```markdown
  ## Summary
  PR-β of the multi-tenant rollout. Builds on PR-α (which added the schema, Argon2id auth, program-owner admin). Makes organizations functional: members can invite people, roles are enforced everywhere, credentials are scoped per-org, and the relay is no longer singleton.

  ## What changed
  - **Invitations**: signed-token issue/verify (7-day expiry), `/settings/members/invite` (Manager 5/hr cap, Owner uncapped), `/settings/members/<id>/revoke`, `/invite/accept` (validates token + creates user + membership + welcome email).
  - **Roles**: `@require_role('owner', 'manager', 'user')` + `@require_program_owner` decorators applied to every state-changing route. Program-owner bypasses org-membership checks. Manager can only invite Users + remove Users. Owner cannot demote self if sole Owner.
  - **Per-org credentials**: `core/secrets_store.py` gains `org_id`; every `secrets_store` call in app code passes `session["current_org_id"]`. Legacy rows with `org_id IS NULL` remain accessible to the program-owner admin only.
  - **Per-org relay**: `_ACCOUNT="default"` removed; `core/relay.py` keys rooms by `org_id`.
  - **Resend live**: `core/email.py` now actually sends, with 3-attempt exponential retry on 5xx/429. Still no-ops with a WARNING log when `RESEND_API_KEY` is unset (dev mode).
  - **Pair flow**: `pair_redeem` requires session; new device rows carry `user_id`.

  ## Test coverage
  - `tests/test_invitations.py`, `tests/test_permissions.py`, `tests/test_members_routes.py`, `tests/test_org_scoped_secrets.py`, `tests/test_org_scoped_relay.py`, `tests/test_email_resend.py`, `tests/test_pwned_passwords.py`, `tests/test_rate_limits.py` (new cases for Manager/Owner cap delta).
  - Existing test suites updated to pass `org_id` where required.

  ## Out of scope (deferred to PR-γ)
  - TOTP + email 2FA + recovery codes + recovery requests
  - Audit log writer + archive job
  - "Forgot password" reset flow

  ## Manual verification
  See Task 24 in the implementation plan: dev-DB walk-through of org-create → invite → accept → role-change → cross-org isolation.
  ```
- [ ] Step 25.3 — Push the branch and open the PR. Do **not** auto-merge — needs design review on the role matrix and a live-Resend smoke from the staging domain (`autoalert.pro` DKIM/SPF/DMARC must be in place before flipping `RESEND_API_KEY` on in prod).
