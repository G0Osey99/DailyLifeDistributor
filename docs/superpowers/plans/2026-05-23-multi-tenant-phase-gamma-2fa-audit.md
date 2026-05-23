# Multi-Tenant Phase γ — 2FA + Recovery + Audit Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Real account security: TOTP (authenticator app) and email-based 2FA with single-use backup recovery codes, an admin-approved out-of-band recovery flow for users who lose everything, login-from-new-device email notifications, org-level "Require 2FA" enforcement, and an audit log of every privileged action with a nightly archive job for entries older than 365 days.

**Architecture:** `core/totp.py` handles RFC 6238 (pyotp); `core/recovery.py` generates and validates backup codes (bcrypt-hashed); `core/email_2fa.py` handles email-code 2FA; `core/audit.py` writes events to `audit_log` table and rotates >365d entries to `audit_log_archive` via a nightly APScheduler job. Login flow grows a second step after password-verify when the user has any 2FA enabled. Every state-changing action in the codebase emits an audit event.

**Tech Stack:** Python 3.11+, Flask, pyotp, qrcode[pil] (QR rendering), bcrypt (recovery code hashing — cheap-to-brute resistant), APScheduler for the nightly archive job, pytest, freezegun for time-control tests.

**Spec:** `docs/superpowers/specs/2026-05-23-multi-tenant-architecture-design.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `requirements.txt` | Add `bcrypt`, `apscheduler`, `freezegun` (test) | Modify |
| `core/totp.py` | RFC 6238 secret gen, provisioning URI, verify, encrypt-for-storage | Create |
| `core/qrcode_render.py` | Render provisioning URI → PNG bytes | Create |
| `core/recovery.py` | Generate/verify/regenerate single-use bcrypt-hashed recovery codes | Create |
| `core/email_2fa.py` | Generate, store-hashed, verify 6-digit email login codes | Create |
| `core/audit.py` | `write_event(...)` writer into `audit_log` | Create |
| `core/audit_archive.py` | Batched nightly archive of rows >365 days | Create |
| `core/login_notifications.py` | First-sighting (user, ip) email notifier | Create |
| `core/recovery_request.py` | Submit/approve admin-approved recovery requests | Create |
| `blueprints/twofa.py` | `/settings/2fa*` routes (enable, verify, disable, recovery codes) | Create |
| `blueprints/auth.py` | Extend login flow with second-factor redirect + `/login/2fa`, `/login/email-2fa` | Modify |
| `blueprints/recovery.py` | `/recover`, `/admin-actions/recovery/<id>/approve`, `/recover/reset` | Create |
| `blueprints/audit.py` | `GET /settings/audit-log` (org-scoped, role-gated) | Create |
| `blueprints/admin.py` | Extend `/admin/audit-log` cross-org search | Modify |
| `blueprints/settings.py` | `/settings/security` preferences + Require-2FA org toggle + enforcement | Modify |
| `templates/settings_2fa.html` | 2FA settings UI | Create |
| `templates/login_2fa.html` | TOTP / recovery code entry form | Create |
| `templates/login_email_2fa.html` | Email-code entry form | Create |
| `templates/recovery_codes.html` | One-shot recovery-codes display + download | Create |
| `templates/audit_log.html` | Audit log table with filters | Create |
| `templates/recover.html` | "I lost everything" submit form | Create |
| `templates/recover_reset.html` | New-password form after approval | Create |
| `templates/email/2fa_code.html` + `.txt` | Email 2FA code | Create |
| `templates/email/recovery_request.html` + `.txt` | To Owners | Create |
| `templates/email/recovery_approved.html` + `.txt` | To requester | Create |
| `templates/email/password_reset.html` + `.txt` | To requester after approval | Create |
| `templates/email/login_new_device.html` + `.txt` | New-device notification | Create |
| `app.py` | Register new blueprints; boot APScheduler | Modify |
| `tests/test_totp.py`, `test_recovery.py`, `test_email_2fa.py`, `test_audit.py`, `test_audit_archive.py`, `test_login_notifications.py`, `test_recovery_flow.py`, `test_twofa_routes.py`, `test_login_2fa.py`, `test_require_2fa.py` | Unit + integration tests | Create |

The `recovery_codes`, `recovery_requests`, and `audit_log` / `audit_log_archive` tables were forward-declared in PR-α; this PR uses them.

---

## Task 1: Add dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Append to `requirements.txt`**

```
bcrypt>=4.1,<5
apscheduler>=3.10,<4
freezegun>=1.4,<2  # test-only
```

- [ ] **Step 2: Install**

Run: `pip install -r requirements.txt`
Expected: `bcrypt`, `APScheduler`, `freezegun` installed without conflicts.

- [ ] **Step 3: Smoke import**

Run: `python -c "import bcrypt, apscheduler, freezegun, pyotp, qrcode; print('ok')"`
Expected: `ok` (pyotp and qrcode are already pulled in by PR-α schema work; if not, append `pyotp>=2.9` and `qrcode[pil]>=7.4`).

- [ ] **Step 4: Commit**

`git add requirements.txt && git commit -m "deps(2fa): add bcrypt, apscheduler, freezegun"`

---

## Task 2: TOTP core — secret + URI + verify

**Files:**
- Create: `core/totp.py`
- Create: `tests/test_totp.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_totp.py
import pyotp
from core import totp


def test_gen_secret_is_base32_16chars():
    s = totp.gen_secret()
    assert len(s) == 16
    # base32 decode must not throw
    import base64
    base64.b32decode(s)


def test_build_provisioning_uri_contains_username_and_issuer():
    uri = totp.build_provisioning_uri("JBSWY3DPEHPK3PXP", "alice")
    assert "alice" in uri
    assert "Daily%20Life%20Distributor" in uri
    assert uri.startswith("otpauth://totp/")


def test_verify_totp_accepts_current_code():
    s = totp.gen_secret()
    code = pyotp.TOTP(s).now()
    assert totp.verify_totp(s, code) is True


def test_verify_totp_rejects_garbage():
    s = totp.gen_secret()
    assert totp.verify_totp(s, "000000") is False
    assert totp.verify_totp(s, "abc") is False
```

Run: `pytest tests/test_totp.py -q`
Expected: `ModuleNotFoundError: No module named 'core.totp'`.

- [ ] **Step 2: Implement `core/totp.py`**

```python
"""RFC 6238 TOTP helpers — gen / URI / verify."""
from __future__ import annotations

import secrets

import pyotp

_ISSUER = "Daily Life Distributor"


def gen_secret() -> str:
    """Return a base32-encoded 16-char secret (80 bits)."""
    # pyotp.random_base32() defaults to 32 chars (160 bits); spec asks for 16.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    return "".join(secrets.choice(alphabet) for _ in range(16))


def build_provisioning_uri(secret: str, username: str, issuer: str = _ISSUER) -> str:
    """Return the otpauth:// URI for QR rendering."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str, drift: int = 1) -> bool:
    """Verify a 6-digit TOTP code with ±drift 30-second steps."""
    if not code or not code.isdigit() or len(code) != 6:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=drift)
```

Run: `pytest tests/test_totp.py -q`
Expected: 4 passed.

- [ ] **Step 3: Commit**

`git add core/totp.py tests/test_totp.py && git commit -m "feat(2fa): TOTP secret/URI/verify primitives"`

---

## Task 3: TOTP secret encryption helpers

**Files:**
- Modify: `core/totp.py`
- Modify: `tests/test_totp.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_totp.py`:

```python
def test_encrypt_decrypt_roundtrip():
    plain = totp.gen_secret()
    enc = totp.encrypt_secret_for_storage(plain)
    assert enc != plain
    assert totp.decrypt_secret_from_storage(enc) == plain


def test_decrypt_garbage_returns_none():
    assert totp.decrypt_secret_from_storage("not-a-fernet-token") is None
```

Run: `pytest tests/test_totp.py::test_encrypt_decrypt_roundtrip -q`
Expected: `AttributeError: module 'core.totp' has no attribute 'encrypt_secret_for_storage'`.

- [ ] **Step 2: Append helpers**

Append to `core/totp.py`:

```python
from core.crypto import get_fernet


def encrypt_secret_for_storage(plaintext_secret: str) -> str:
    """Encrypt a TOTP secret using the app's Fernet master key."""
    f = get_fernet()
    return f.encrypt(plaintext_secret.encode("utf-8")).decode("ascii")


def decrypt_secret_from_storage(ciphertext: str) -> str | None:
    """Decrypt; return None on any failure (corrupt / wrong key)."""
    try:
        f = get_fernet()
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception:
        return None
```

Run: `pytest tests/test_totp.py -q`
Expected: 6 passed.

- [ ] **Step 3: Commit**

`git add core/totp.py tests/test_totp.py && git commit -m "feat(2fa): Fernet-encrypted TOTP secret storage"`

---

## Task 4: QR-code PNG rendering

**Files:**
- Create: `core/qrcode_render.py`
- Create: `tests/test_qrcode_render.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_qrcode_render.py
from core.qrcode_render import render_provisioning_qr_png


def test_render_returns_png_bytes():
    data = render_provisioning_qr_png(
        "otpauth://totp/Daily%20Life%20Distributor:alice?secret=ABCD&issuer=Daily%20Life%20Distributor"
    )
    assert isinstance(data, (bytes, bytearray))
    # PNG signature
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(data) > 200
```

Run: `pytest tests/test_qrcode_render.py -q`
Expected: `ModuleNotFoundError`.

- [ ] **Step 2: Implement**

```python
# core/qrcode_render.py
"""Render an otpauth:// provisioning URI as PNG bytes."""
from __future__ import annotations

import io

import qrcode


def render_provisioning_qr_png(uri: str) -> bytes:
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
```

Run: `pytest tests/test_qrcode_render.py -q`
Expected: 1 passed.

- [ ] **Step 3: Commit**

`git add core/qrcode_render.py tests/test_qrcode_render.py && git commit -m "feat(2fa): QR code PNG renderer for provisioning URIs"`

---

## Task 5: GET /settings/2fa — status page

**Files:**
- Create: `blueprints/twofa.py`
- Create: `templates/settings_2fa.html`
- Modify: `app.py` (register blueprint)
- Create: `tests/test_twofa_routes.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_twofa_routes.py
from tests.helpers import login_as, make_user


def test_get_settings_2fa_shows_disabled_state(client, db):
    user = make_user(db, username="alice", totp_enabled=False, email_2fa_enabled=False)
    login_as(client, user)
    resp = client.get("/settings/2fa")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Authenticator app" in body
    assert "Enable TOTP" in body
    assert "Enable email codes" in body


def test_get_settings_2fa_shows_enabled_state(client, db):
    user = make_user(db, username="bob", totp_enabled=True, email_2fa_enabled=True)
    login_as(client, user)
    resp = client.get("/settings/2fa")
    body = resp.get_data(as_text=True)
    assert "Disable" in body
    assert "Authenticator app: enabled" in body
```

Run: `pytest tests/test_twofa_routes.py::test_get_settings_2fa_shows_disabled_state -q`
Expected: `404` (no blueprint registered).

- [ ] **Step 2: Create blueprint stub + GET route**

```python
# blueprints/twofa.py
from __future__ import annotations

from flask import Blueprint, render_template, session

from blueprints.auth import login_required
from core import db as _db

bp = Blueprint("twofa", __name__)


def _current_user() -> dict:
    uid = session.get("user_id")
    return _db.get_user_by_id(uid)


@bp.get("/settings/2fa")
@login_required
def settings_2fa():
    user = _current_user()
    return render_template(
        "settings_2fa.html",
        totp_enabled=bool(user.get("totp_enabled")),
        email_2fa_enabled=bool(user.get("email_2fa_enabled")),
    )
```

- [ ] **Step 3: Create template**

```html
{# templates/settings_2fa.html #}
{% extends "base.html" %}
{% block content %}
<h1>Two-factor authentication</h1>

<section>
  <h2>Authenticator app: {{ "enabled" if totp_enabled else "disabled" }}</h2>
  {% if totp_enabled %}
    <form method="post" action="/settings/2fa/disable">
      <input type="hidden" name="method" value="totp">
      <label>Enter current 6-digit code to confirm:
        <input name="code" pattern="[0-9]{6}" required>
      </label>
      <button type="submit">Disable TOTP</button>
    </form>
  {% else %}
    <form method="post" action="/settings/2fa/enable-totp">
      <button type="submit">Enable TOTP</button>
    </form>
  {% endif %}
</section>

<section>
  <h2>Email codes: {{ "enabled" if email_2fa_enabled else "disabled" }}</h2>
  {% if email_2fa_enabled %}
    <form method="post" action="/settings/2fa/disable">
      <input type="hidden" name="method" value="email">
      <button type="submit">Disable email codes</button>
    </form>
  {% else %}
    <form method="post" action="/settings/2fa/enable-email">
      <button type="submit">Enable email codes</button>
    </form>
  {% endif %}
</section>

<p><a href="/settings/2fa/recovery-codes">Recovery codes</a></p>
{% endblock %}
```

- [ ] **Step 4: Register blueprint in `app.py`**

In `app.py` `create_app()`, after the existing `app.register_blueprint(auth_bp)`:

```python
from blueprints.twofa import bp as twofa_bp
app.register_blueprint(twofa_bp)
```

Run: `pytest tests/test_twofa_routes.py -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

`git add blueprints/twofa.py templates/settings_2fa.html app.py tests/test_twofa_routes.py && git commit -m "feat(2fa): /settings/2fa status page"`

---

## Task 6: POST /settings/2fa/enable-totp — generate + show QR

**Files:**
- Modify: `blueprints/twofa.py`
- Modify: `templates/settings_2fa.html` (add QR display branch via redirect target template)
- Create: `templates/settings_2fa_totp_setup.html`
- Modify: `tests/test_twofa_routes.py`

- [ ] **Step 1: Failing test**

```python
def test_enable_totp_renders_qr_and_pending_secret(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    resp = client.post("/settings/2fa/enable-totp", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Scan with your authenticator app" in body
    assert '<img src="/settings/2fa/qrcode.png"' in body
    # secret stashed in session (encrypted)
    with client.session_transaction() as s:
        assert s["pending_totp_secret_enc"]
```

Run: `pytest tests/test_twofa_routes.py::test_enable_totp_renders_qr_and_pending_secret -q`
Expected: 404 / no route.

- [ ] **Step 2: Add the route + QR endpoint**

Append to `blueprints/twofa.py`:

```python
from flask import Response, request, redirect, url_for, flash

from core import totp as _totp
from core.qrcode_render import render_provisioning_qr_png


@bp.post("/settings/2fa/enable-totp")
@login_required
def enable_totp():
    user = _current_user()
    secret = _totp.gen_secret()
    enc = _totp.encrypt_secret_for_storage(secret)
    session["pending_totp_secret_enc"] = enc
    uri = _totp.build_provisioning_uri(secret, user["username"])
    session["pending_totp_uri"] = uri
    return render_template("settings_2fa_totp_setup.html")


@bp.get("/settings/2fa/qrcode.png")
@login_required
def totp_qrcode():
    uri = session.get("pending_totp_uri")
    if not uri:
        return ("", 404)
    return Response(render_provisioning_qr_png(uri), mimetype="image/png")
```

- [ ] **Step 3: Create setup template**

```html
{# templates/settings_2fa_totp_setup.html #}
{% extends "base.html" %}
{% block content %}
<h1>Scan with your authenticator app</h1>
<img src="/settings/2fa/qrcode.png" alt="TOTP QR code">
<form method="post" action="/settings/2fa/verify-totp">
  <label>Enter the 6-digit code from your app:
    <input name="code" pattern="[0-9]{6}" required autofocus>
  </label>
  <button type="submit">Verify and enable</button>
</form>
<p><a href="/settings/2fa">Cancel</a></p>
{% endblock %}
```

Run: `pytest tests/test_twofa_routes.py -q`
Expected: passes including new test.

- [ ] **Step 4: Commit**

`git add blueprints/twofa.py templates/settings_2fa_totp_setup.html tests/test_twofa_routes.py && git commit -m "feat(2fa): enable-totp generates secret + renders QR"`

---

## Task 7: POST /settings/2fa/verify-totp — flip enabled + show codes

**Files:**
- Modify: `blueprints/twofa.py`
- Modify: `core/db.py` (add `set_user_totp(user_id, encrypted_secret, enabled)`)
- Modify: `tests/test_twofa_routes.py`

- [ ] **Step 1: Failing test**

```python
import pyotp
from core import totp as _totp


def test_verify_totp_good_code_enables_and_shows_recovery(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    client.post("/settings/2fa/enable-totp")
    with client.session_transaction() as s:
        enc = s["pending_totp_secret_enc"]
    secret = _totp.decrypt_secret_from_storage(enc)
    code = pyotp.TOTP(secret).now()
    resp = client.post("/settings/2fa/verify-totp", data={"code": code})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Save these recovery codes" in body
    row = db.get_user_by_id(user["id"])
    assert row["totp_enabled"] == 1
    assert row["totp_secret_encrypted"] == enc


def test_verify_totp_bad_code_does_not_enable(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    client.post("/settings/2fa/enable-totp")
    resp = client.post("/settings/2fa/verify-totp", data={"code": "000000"})
    assert resp.status_code == 400
    row = db.get_user_by_id(user["id"])
    assert row["totp_enabled"] == 0
```

- [ ] **Step 2: Add `db.set_user_totp`**

Append to `core/db.py`:

```python
def set_user_totp(user_id: int, encrypted_secret: str | None, enabled: bool) -> None:
    with _connect() as cx:
        cx.execute(
            "UPDATE users SET totp_secret_encrypted=?, totp_enabled=? WHERE id=?",
            (encrypted_secret, 1 if enabled else 0, user_id),
        )
```

- [ ] **Step 3: Implement verify route**

Append to `blueprints/twofa.py`:

```python
from core import recovery as _recovery


@bp.post("/settings/2fa/verify-totp")
@login_required
def verify_totp_post():
    user = _current_user()
    enc = session.get("pending_totp_secret_enc")
    code = (request.form.get("code") or "").strip()
    if not enc:
        return ("Setup not started", 400)
    secret = _totp.decrypt_secret_from_storage(enc)
    if not secret or not _totp.verify_totp(secret, code):
        return render_template("settings_2fa_totp_setup.html", error="Invalid code"), 400
    _db.set_user_totp(user["id"], enc, enabled=True)
    session.pop("pending_totp_secret_enc", None)
    session.pop("pending_totp_uri", None)
    codes = _recovery.generate_recovery_codes(user["id"])
    return render_template("recovery_codes.html", codes=codes, first_time=True)
```

- [ ] **Step 4: Create `templates/recovery_codes.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>Save these recovery codes</h1>
<p>Each code can be used once. Store them somewhere safe — you will not see them again.</p>
<ul class="recovery-codes">
  {% for c in codes %}<li><code>{{ c }}</code></li>{% endfor %}
</ul>
<form method="get" action="/settings/2fa/recovery-codes/download">
  <button type="submit">Download as .txt</button>
</form>
<p><a href="/settings/2fa">Done</a></p>
{% endblock %}
```

Run: `pytest tests/test_twofa_routes.py -q`
Expected: all pass (requires Task 11's `recovery.generate_recovery_codes` — implement it first if running tests strictly in order; otherwise stub it as `return ["XXXX1111"] * 10` temporarily and replace in Task 11).

- [ ] **Step 5: Commit**

`git add core/db.py blueprints/twofa.py templates/recovery_codes.html tests/test_twofa_routes.py && git commit -m "feat(2fa): verify-totp flips enabled and shows recovery codes"`

---

## Task 8: POST /settings/2fa/enable-email — flip + send test code

**Files:**
- Modify: `blueprints/twofa.py`
- Modify: `core/db.py` (add `set_user_email_2fa(user_id, enabled)`)
- Create: `core/email_2fa.py` (stub `generate_login_code` for now — fully implemented in Task 17)
- Modify: `tests/test_twofa_routes.py`

- [ ] **Step 1: Failing test**

```python
def test_enable_email_2fa_flips_flag_and_sends(client, db, captured_emails):
    user = make_user(db, username="alice", email="alice@example.com")
    login_as(client, user)
    resp = client.post("/settings/2fa/enable-email")
    assert resp.status_code in (200, 302)
    assert db.get_user_by_id(user["id"])["email_2fa_enabled"] == 1
    assert any(m["template"] == "2fa_code" and "alice@example.com" in m["to"] for m in captured_emails)
```

(`captured_emails` is a pytest fixture established in PR-β that records `core.email.send` calls.)

- [ ] **Step 2: `db.set_user_email_2fa`**

Append to `core/db.py`:

```python
def set_user_email_2fa(user_id: int, enabled: bool) -> None:
    with _connect() as cx:
        cx.execute("UPDATE users SET email_2fa_enabled=? WHERE id=?", (1 if enabled else 0, user_id))
```

- [ ] **Step 3: Route**

Append to `blueprints/twofa.py`:

```python
from core import email_2fa as _email_2fa


@bp.post("/settings/2fa/enable-email")
@login_required
def enable_email_2fa():
    user = _current_user()
    _db.set_user_email_2fa(user["id"], True)
    _email_2fa.generate_login_code(user["id"])  # sends test code
    flash("Email 2FA enabled. A test code was sent to your email.")
    return redirect(url_for("twofa.settings_2fa"))
```

Run: `pytest tests/test_twofa_routes.py -q`
Expected: passes (requires Task 17 stub).

- [ ] **Step 4: Commit**

`git add core/db.py blueprints/twofa.py tests/test_twofa_routes.py && git commit -m "feat(2fa): enable-email flips flag and sends test code"`

---

## Task 9: POST /settings/2fa/disable — confirm before disabling

**Files:**
- Modify: `blueprints/twofa.py`
- Modify: `tests/test_twofa_routes.py`

- [ ] **Step 1: Failing tests**

```python
def test_disable_totp_requires_current_code(client, db):
    user = make_user(db, username="alice", totp_enabled=True,
                     totp_secret_encrypted=_totp.encrypt_secret_for_storage("JBSWY3DPEHPK3PXP"))
    login_as(client, user)
    resp = client.post("/settings/2fa/disable", data={"method": "totp", "code": "000000"})
    assert resp.status_code == 400
    assert db.get_user_by_id(user["id"])["totp_enabled"] == 1


def test_disable_totp_with_valid_code_clears_secret(client, db):
    secret = "JBSWY3DPEHPK3PXP"
    user = make_user(db, username="alice", totp_enabled=True,
                     totp_secret_encrypted=_totp.encrypt_secret_for_storage(secret))
    login_as(client, user)
    code = pyotp.TOTP(secret).now()
    resp = client.post("/settings/2fa/disable", data={"method": "totp", "code": code})
    assert resp.status_code in (200, 302)
    row = db.get_user_by_id(user["id"])
    assert row["totp_enabled"] == 0
    assert row["totp_secret_encrypted"] is None
```

- [ ] **Step 2: Route**

```python
@bp.post("/settings/2fa/disable")
@login_required
def disable_2fa():
    user = _current_user()
    method = (request.form.get("method") or "").strip()
    code = (request.form.get("code") or "").strip()
    if method == "totp":
        enc = user.get("totp_secret_encrypted")
        secret = _totp.decrypt_secret_from_storage(enc) if enc else None
        if not secret or not _totp.verify_totp(secret, code):
            return render_template("settings_2fa.html",
                                   totp_enabled=True,
                                   email_2fa_enabled=bool(user.get("email_2fa_enabled")),
                                   error="Invalid code"), 400
        _db.set_user_totp(user["id"], None, enabled=False)
    elif method == "email":
        # Optionally require an emailed confirmation code; simplest: trust the session.
        _db.set_user_email_2fa(user["id"], False)
    else:
        return ("Unknown method", 400)
    flash("Two-factor authentication disabled.")
    return redirect(url_for("twofa.settings_2fa"))
```

Run: `pytest tests/test_twofa_routes.py -q`
Expected: pass.

- [ ] **Step 3: Commit**

`git add blueprints/twofa.py tests/test_twofa_routes.py && git commit -m "feat(2fa): disable-2fa requires current code"`

---

## Task 10: Recovery codes core — generate + bcrypt store

**Files:**
- Create: `core/recovery.py`
- Create: `tests/test_recovery.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_recovery.py
from core import recovery


def test_generate_returns_10_distinct_plain_codes(db):
    user = make_user(db, username="alice")
    codes = recovery.generate_recovery_codes(user["id"])
    assert len(codes) == 10
    assert len(set(codes)) == 10
    for c in codes:
        assert len(c) == 8
        assert c.isalnum()


def test_codes_stored_hashed_not_plain(db):
    user = make_user(db, username="alice")
    codes = recovery.generate_recovery_codes(user["id"])
    rows = db.list_recovery_codes(user["id"])
    assert len(rows) == 10
    for plain, row in zip(codes, rows):
        assert plain not in row["code_hash"]
        assert row["code_hash"].startswith("$2b$")  # bcrypt prefix
        assert row["used_at"] is None
```

Run: `pytest tests/test_recovery.py -q`
Expected: ModuleNotFoundError.

- [ ] **Step 2: Implement**

```python
# core/recovery.py
"""Backup recovery codes — generate, verify, regenerate. bcrypt-hashed."""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone

import bcrypt

from core import db as _db

_ALPHABET = string.ascii_uppercase + string.digits
_CODE_LEN = 8
_CODE_COUNT = 10


def _new_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))


def generate_recovery_codes(user_id: int, count: int = _CODE_COUNT) -> list[str]:
    """Mint `count` codes, store bcrypt-hashed, return plain codes ONCE."""
    plain = [_new_code() for _ in range(count)]
    now = datetime.now(timezone.utc).isoformat()
    for p in plain:
        h = bcrypt.hashpw(p.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii")
        _db.insert_recovery_code(user_id=user_id, code_hash=h, created_at=now)
    return plain
```

- [ ] **Step 3: Add db helpers**

Append to `core/db.py`:

```python
def insert_recovery_code(*, user_id: int, code_hash: str, created_at: str) -> int:
    with _connect() as cx:
        cur = cx.execute(
            "INSERT INTO recovery_codes (user_id, code_hash, created_at) VALUES (?, ?, ?)",
            (user_id, code_hash, created_at),
        )
        return cur.lastrowid


def list_recovery_codes(user_id: int) -> list[dict]:
    with _connect() as cx:
        rows = cx.execute(
            "SELECT id, code_hash, used_at, created_at FROM recovery_codes WHERE user_id=? ORDER BY id",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]
```

Run: `pytest tests/test_recovery.py -q`
Expected: 2 pass.

- [ ] **Step 4: Commit**

`git add core/recovery.py core/db.py tests/test_recovery.py && git commit -m "feat(recovery): generate bcrypt-hashed backup codes"`

---

## Task 11: Recovery code verify + single-use

**Files:**
- Modify: `core/recovery.py`
- Modify: `tests/test_recovery.py`

- [ ] **Step 1: Failing test**

```python
def test_verify_correct_code_marks_used_and_second_use_fails(db):
    user = make_user(db, username="alice")
    codes = recovery.generate_recovery_codes(user["id"])
    one = codes[0]
    assert recovery.verify_recovery_code(user["id"], one) is True
    assert recovery.verify_recovery_code(user["id"], one) is False


def test_verify_unknown_code_returns_false(db):
    user = make_user(db, username="alice")
    recovery.generate_recovery_codes(user["id"])
    assert recovery.verify_recovery_code(user["id"], "AAAAAAAA") is False


def test_verify_other_users_code_returns_false(db):
    a = make_user(db, username="a")
    b = make_user(db, username="b")
    codes_a = recovery.generate_recovery_codes(a["id"])
    assert recovery.verify_recovery_code(b["id"], codes_a[0]) is False
```

- [ ] **Step 2: Implement**

Append to `core/recovery.py`:

```python
def verify_recovery_code(user_id: int, code: str) -> bool:
    """Return True iff code matches an unused row; mark it used atomically."""
    if not code:
        return False
    candidate = code.strip().upper().encode("utf-8")
    rows = _db.list_recovery_codes(user_id)
    for row in rows:
        if row["used_at"] is not None:
            continue
        if bcrypt.checkpw(candidate, row["code_hash"].encode("ascii")):
            _db.mark_recovery_code_used(row["id"])
            return True
    return False
```

Append to `core/db.py`:

```python
def mark_recovery_code_used(code_id: int) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as cx:
        cx.execute("UPDATE recovery_codes SET used_at=? WHERE id=? AND used_at IS NULL", (now, code_id))
```

Run: `pytest tests/test_recovery.py -q`
Expected: 5 pass.

- [ ] **Step 3: Commit**

`git add core/recovery.py core/db.py tests/test_recovery.py && git commit -m "feat(recovery): single-use verify of recovery codes"`

---

## Task 12: Regenerate codes

**Files:**
- Modify: `core/recovery.py`
- Modify: `tests/test_recovery.py`

- [ ] **Step 1: Failing test**

```python
def test_regenerate_invalidates_old_codes(db):
    user = make_user(db, username="alice")
    old = recovery.generate_recovery_codes(user["id"])
    new = recovery.regenerate_codes(user["id"])
    assert set(old).isdisjoint(set(new))
    # All old codes now reject (marked invalidated / replaced)
    for c in old:
        assert recovery.verify_recovery_code(user["id"], c) is False
    # New code works
    assert recovery.verify_recovery_code(user["id"], new[0]) is True
```

- [ ] **Step 2: Implement**

Append to `core/recovery.py`:

```python
def regenerate_codes(user_id: int) -> list[str]:
    _db.delete_recovery_codes(user_id)
    return generate_recovery_codes(user_id)
```

Append to `core/db.py`:

```python
def delete_recovery_codes(user_id: int) -> None:
    with _connect() as cx:
        cx.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))
```

Run: `pytest tests/test_recovery.py -q`
Expected: 6 pass.

- [ ] **Step 3: Commit**

`git add core/recovery.py core/db.py tests/test_recovery.py && git commit -m "feat(recovery): regenerate_codes purges + mints fresh"`

---

## Task 13: GET /settings/2fa/recovery-codes + POST regenerate

**Files:**
- Modify: `blueprints/twofa.py`
- Modify: `tests/test_twofa_routes.py`

- [ ] **Step 1: Failing tests**

```python
def test_get_recovery_codes_does_not_show_plaintext(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    _ = recovery.generate_recovery_codes(user["id"])
    resp = client.get("/settings/2fa/recovery-codes")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Recovery codes" in body
    assert "<code>" not in body  # no plaintext re-display
    assert "Regenerate" in body


def test_post_regenerate_shows_new_codes_once(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    recovery.generate_recovery_codes(user["id"])
    resp = client.post("/settings/2fa/recovery-codes/regenerate")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert body.count("<code>") == 10
```

- [ ] **Step 2: Routes**

Append to `blueprints/twofa.py`:

```python
@bp.get("/settings/2fa/recovery-codes")
@login_required
def recovery_codes_view():
    user = _current_user()
    codes = _db.list_recovery_codes(user["id"])
    remaining = sum(1 for c in codes if c["used_at"] is None)
    return render_template("recovery_codes.html",
                           codes=None,
                           remaining=remaining,
                           first_time=False)


@bp.post("/settings/2fa/recovery-codes/regenerate")
@login_required
def recovery_codes_regenerate():
    user = _current_user()
    codes = _recovery.regenerate_codes(user["id"])
    return render_template("recovery_codes.html", codes=codes, first_time=False)
```

Update `templates/recovery_codes.html` to handle the no-plaintext branch:

```html
{# add at the top of the {% block content %} #}
{% if codes %}
  {# ... existing display ... #}
{% else %}
  <h1>Recovery codes</h1>
  <p>{{ remaining }} of 10 codes remaining. Codes cannot be re-displayed once generated.</p>
  <form method="post" action="/settings/2fa/recovery-codes/regenerate">
    <button type="submit">Regenerate codes</button>
  </form>
{% endif %}
```

Run: `pytest tests/test_twofa_routes.py -q`
Expected: passes.

- [ ] **Step 3: Commit**

`git add blueprints/twofa.py templates/recovery_codes.html tests/test_twofa_routes.py && git commit -m "feat(2fa): recovery-codes view + regenerate route"`

---

## Task 14: Login flow — redirect to 2FA when enabled

**Files:**
- Modify: `blueprints/auth.py`
- Create: `tests/test_login_2fa.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_login_2fa.py
from itsdangerous import URLSafeTimedSerializer
from tests.helpers import make_user


def test_password_only_user_logs_in_directly(client, db):
    make_user(db, username="alice", password="hunter22hunter22")
    resp = client.post("/login", data={"username": "alice", "password": "hunter22hunter22"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")  # dashboard


def test_totp_user_redirected_to_login_2fa(client, db, app):
    make_user(db, username="bob", password="hunter22hunter22", totp_enabled=True)
    resp = client.post("/login", data={"username": "bob", "password": "hunter22hunter22"})
    assert resp.status_code == 302
    assert "/login/2fa" in resp.headers["Location"]
    # token in URL is signed and carries user_id
    qs = resp.headers["Location"].split("?", 1)[1]
    assert "tok=" in qs


def test_email_only_user_redirected_to_email_2fa(client, db):
    make_user(db, username="eve", password="hunter22hunter22",
              totp_enabled=False, email_2fa_enabled=True)
    resp = client.post("/login", data={"username": "eve", "password": "hunter22hunter22"})
    assert resp.status_code == 302
    assert "/login/email-2fa" in resp.headers["Location"]
```

- [ ] **Step 2: Modify `blueprints/auth.py`**

Inside `login_submit()` after a successful password verify, before setting the full session, replace the success path with:

```python
from itsdangerous import URLSafeTimedSerializer
from flask import current_app


def _partial_token(user_id: int) -> str:
    s = URLSafeTimedSerializer(current_app.secret_key, salt="2fa-pending")
    return s.dumps({"uid": user_id})


def _consume_partial_token(tok: str) -> int | None:
    s = URLSafeTimedSerializer(current_app.secret_key, salt="2fa-pending")
    try:
        data = s.loads(tok, max_age=300)
        return int(data["uid"])
    except Exception:
        return None
```

Then in `login_submit()` replace the existing "set session, redirect" success block:

```python
user = _db.get_user_by_username(username)
if user.get("totp_enabled") or user.get("email_2fa_enabled"):
    tok = _partial_token(user["id"])
    if user.get("totp_enabled"):
        return redirect(url_for("auth.login_2fa") + f"?tok={tok}")
    return redirect(url_for("auth.login_email_2fa") + f"?tok={tok}")
# No 2FA — finalize session as before
session.clear()
session["user_id"] = user["id"]
session["authenticated"] = True
session.permanent = True
return redirect(_safe_next(request.form.get("next", "/")))
```

Run: `pytest tests/test_login_2fa.py -q`
Expected: passes.

- [ ] **Step 3: Commit**

`git add blueprints/auth.py tests/test_login_2fa.py && git commit -m "feat(login): redirect to 2FA step after password verify"`

---

## Task 15: GET/POST /login/2fa — TOTP or recovery code

**Files:**
- Modify: `blueprints/auth.py`
- Create: `templates/login_2fa.html`
- Modify: `tests/test_login_2fa.py`

- [ ] **Step 1: Failing tests**

```python
def test_login_2fa_post_totp_finalizes_session(client, db):
    import pyotp
    from core import totp as _totp
    secret = "JBSWY3DPEHPK3PXP"
    user = make_user(db, username="bob", password="hunter22hunter22",
                     totp_enabled=True,
                     totp_secret_encrypted=_totp.encrypt_secret_for_storage(secret))
    resp = client.post("/login", data={"username": "bob", "password": "hunter22hunter22"})
    tok = resp.headers["Location"].split("tok=")[1]
    code = pyotp.TOTP(secret).now()
    r2 = client.post("/login/2fa", data={"tok": tok, "code": code})
    assert r2.status_code == 302
    with client.session_transaction() as s:
        assert s["user_id"] == user["id"]
        assert s["authenticated"] is True


def test_login_2fa_post_recovery_code_finalizes_and_marks_used(client, db):
    from core import recovery, totp as _totp
    secret = "JBSWY3DPEHPK3PXP"
    user = make_user(db, username="bob", password="hunter22hunter22",
                     totp_enabled=True,
                     totp_secret_encrypted=_totp.encrypt_secret_for_storage(secret))
    codes = recovery.generate_recovery_codes(user["id"])
    resp = client.post("/login", data={"username": "bob", "password": "hunter22hunter22"})
    tok = resp.headers["Location"].split("tok=")[1]
    r2 = client.post("/login/2fa", data={"tok": tok, "code": codes[0]})
    assert r2.status_code == 302
    # Same code rejected on second try
    resp = client.post("/login", data={"username": "bob", "password": "hunter22hunter22"})
    tok2 = resp.headers["Location"].split("tok=")[1]
    r3 = client.post("/login/2fa", data={"tok": tok2, "code": codes[0]})
    assert r3.status_code == 400
```

- [ ] **Step 2: Routes**

Append to `blueprints/auth.py`:

```python
from core import totp as _totp
from core import recovery as _recovery


@bp.get("/login/2fa")
def login_2fa_get():
    tok = request.args.get("tok", "")
    uid = _consume_partial_token(tok)
    if uid is None:
        return redirect(url_for("auth.login"))
    return render_template("login_2fa.html", tok=tok)


@bp.post("/login/2fa")
def login_2fa_post():
    tok = request.form.get("tok", "")
    uid = _consume_partial_token(tok)
    if uid is None:
        return redirect(url_for("auth.login"))
    code = (request.form.get("code") or "").strip()
    user = _db.get_user_by_id(uid)
    enc = user.get("totp_secret_encrypted")
    secret = _totp.decrypt_secret_from_storage(enc) if enc else None
    ok = bool(secret and _totp.verify_totp(secret, code))
    if not ok:
        ok = _recovery.verify_recovery_code(uid, code)
    if not ok:
        return render_template("login_2fa.html", tok=tok, error="Invalid code"), 400
    session.clear()
    session["user_id"] = uid
    session["authenticated"] = True
    session.permanent = True
    return redirect("/")
```

- [ ] **Step 3: Template**

```html
{# templates/login_2fa.html #}
{% extends "base.html" %}
{% block content %}
<h1>Two-factor authentication</h1>
<form method="post" action="/login/2fa">
  <input type="hidden" name="tok" value="{{ tok }}">
  <label>Enter the 6-digit code from your app, or a recovery code:
    <input name="code" required autofocus autocomplete="one-time-code">
  </label>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <button type="submit">Continue</button>
</form>
{% endblock %}
```

Run: `pytest tests/test_login_2fa.py -q`
Expected: passes.

- [ ] **Step 4: Commit**

`git add blueprints/auth.py templates/login_2fa.html tests/test_login_2fa.py && git commit -m "feat(login): /login/2fa accepts TOTP or recovery code"`

---

## Task 16: GET/POST /login/email-2fa — email code path

**Files:**
- Modify: `blueprints/auth.py`
- Create: `templates/login_email_2fa.html`
- Modify: `tests/test_login_2fa.py`

- [ ] **Step 1: Failing test**

```python
def test_login_email_2fa_flow(client, db, captured_emails):
    user = make_user(db, username="eve", password="hunter22hunter22", email_2fa_enabled=True,
                     email="eve@example.com")
    resp = client.post("/login", data={"username": "eve", "password": "hunter22hunter22"})
    tok = resp.headers["Location"].split("tok=")[1]
    # GET issues a code
    r1 = client.get(f"/login/email-2fa?tok={tok}")
    assert r1.status_code == 200
    msg = next(m for m in captured_emails if m["template"] == "2fa_code")
    code = msg["vars"]["code"]
    # POST verifies
    r2 = client.post("/login/email-2fa", data={"tok": tok, "code": code})
    assert r2.status_code == 302
    with client.session_transaction() as s:
        assert s["user_id"] == user["id"]
```

- [ ] **Step 2: Routes**

Append to `blueprints/auth.py`:

```python
from core import email_2fa as _email_2fa


@bp.get("/login/email-2fa")
def login_email_2fa_get():
    tok = request.args.get("tok", "")
    uid = _consume_partial_token(tok)
    if uid is None:
        return redirect(url_for("auth.login"))
    _email_2fa.generate_login_code(uid)
    return render_template("login_email_2fa.html", tok=tok)


@bp.post("/login/email-2fa")
def login_email_2fa_post():
    tok = request.form.get("tok", "")
    uid = _consume_partial_token(tok)
    if uid is None:
        return redirect(url_for("auth.login"))
    code = (request.form.get("code") or "").strip()
    if not _email_2fa.verify_login_code(uid, code):
        return render_template("login_email_2fa.html", tok=tok, error="Invalid code"), 400
    session.clear()
    session["user_id"] = uid
    session["authenticated"] = True
    session.permanent = True
    return redirect("/")
```

- [ ] **Step 3: Template**

```html
{# templates/login_email_2fa.html #}
{% extends "base.html" %}
{% block content %}
<h1>Check your email</h1>
<p>We sent a 6-digit code to your email. It expires in 10 minutes.</p>
<form method="post" action="/login/email-2fa">
  <input type="hidden" name="tok" value="{{ tok }}">
  <label>Code: <input name="code" pattern="[0-9]{6}" required autofocus autocomplete="one-time-code"></label>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <button type="submit">Continue</button>
</form>
{% endblock %}
```

Run: `pytest tests/test_login_2fa.py -q`
Expected: passes.

- [ ] **Step 4: Commit**

`git add blueprints/auth.py templates/login_email_2fa.html tests/test_login_2fa.py && git commit -m "feat(login): /login/email-2fa issues + verifies email code"`

---

## Task 17: core/email_2fa.py — hashed-store 6-digit codes

**Files:**
- Create: `core/email_2fa.py`
- Modify: `core/db.py` (table + helpers)
- Create: `tests/test_email_2fa.py`

- [ ] **Step 1: Add `email_2fa_codes` table to schema**

In `core/db.py` `init_db()`:

```python
cx.executescript("""
CREATE TABLE IF NOT EXISTS email_2fa_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_email_2fa_codes_user ON email_2fa_codes(user_id, used_at);
""")
```

Append helpers:

```python
def insert_email_2fa_code(*, user_id: int, code_hash: str, expires_at: str, created_at: str) -> int:
    with _connect() as cx:
        cur = cx.execute(
            "INSERT INTO email_2fa_codes (user_id, code_hash, expires_at, created_at) VALUES (?,?,?,?)",
            (user_id, code_hash, expires_at, created_at),
        )
        return cur.lastrowid


def get_unused_email_2fa_codes(user_id: int) -> list[dict]:
    with _connect() as cx:
        rows = cx.execute(
            "SELECT id, code_hash, expires_at FROM email_2fa_codes "
            "WHERE user_id=? AND used_at IS NULL ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_email_2fa_code_used(code_id: int) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as cx:
        cx.execute("UPDATE email_2fa_codes SET used_at=? WHERE id=?", (now, code_id))
```

- [ ] **Step 2: Failing tests**

```python
# tests/test_email_2fa.py
from datetime import timedelta
from freezegun import freeze_time

from core import email_2fa


def test_generate_sends_and_stores_hash(db, captured_emails):
    user = make_user(db, username="eve", email="eve@example.com")
    code = email_2fa.generate_login_code(user["id"])
    assert len(code) == 6 and code.isdigit()
    msg = captured_emails[-1]
    assert msg["template"] == "2fa_code"
    assert "eve@example.com" in msg["to"]
    assert msg["vars"]["code"] == code
    rows = db.get_unused_email_2fa_codes(user["id"])
    assert len(rows) == 1
    assert code not in rows[0]["code_hash"]  # hashed


def test_verify_correct_code(db):
    user = make_user(db, username="eve", email="eve@example.com")
    code = email_2fa.generate_login_code(user["id"])
    assert email_2fa.verify_login_code(user["id"], code) is True
    # Second use rejected (single-use)
    assert email_2fa.verify_login_code(user["id"], code) is False


def test_verify_expired_code():
    pass  # implemented via freezegun below


def test_verify_expired_code_returns_false(db):
    user = make_user(db, username="eve", email="eve@example.com")
    with freeze_time("2026-05-23 12:00:00"):
        code = email_2fa.generate_login_code(user["id"])
    with freeze_time("2026-05-23 12:11:00"):  # 11 minutes later
        assert email_2fa.verify_login_code(user["id"], code) is False
```

- [ ] **Step 3: Implement `core/email_2fa.py`**

```python
"""Email-based 6-digit single-use 2FA codes."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt

from core import db as _db
from core import email as _email

_TTL = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_login_code(user_id: int) -> str:
    code = f"{secrets.randbelow(10**6):06d}"
    h = bcrypt.hashpw(code.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii")
    now = _now()
    _db.insert_email_2fa_code(
        user_id=user_id,
        code_hash=h,
        expires_at=(now + _TTL).isoformat(),
        created_at=now.isoformat(),
    )
    user = _db.get_user_by_id(user_id)
    _email.send("2fa_code", to=user["email"], code=code, username=user["username"])
    return code


def verify_login_code(user_id: int, code: str) -> bool:
    if not code or not code.isdigit() or len(code) != 6:
        return False
    candidate = code.encode("utf-8")
    rows = _db.get_unused_email_2fa_codes(user_id)
    now_iso = _now().isoformat()
    for row in rows:
        if row["expires_at"] < now_iso:
            continue
        if bcrypt.checkpw(candidate, row["code_hash"].encode("ascii")):
            _db.mark_email_2fa_code_used(row["id"])
            return True
    return False
```

Run: `pytest tests/test_email_2fa.py -q`
Expected: passes.

- [ ] **Step 4: Commit**

`git add core/email_2fa.py core/db.py tests/test_email_2fa.py && git commit -m "feat(2fa): email 6-digit code (hashed, single-use, 10min TTL)"`

---

## Task 18: Email templates for 2FA + recovery + new device

**Files:**
- Create: `templates/email/2fa_code.html`
- Create: `templates/email/2fa_code.txt`
- Create: `templates/email/recovery_request.html`
- Create: `templates/email/recovery_request.txt`
- Create: `templates/email/recovery_approved.html`
- Create: `templates/email/recovery_approved.txt`
- Create: `templates/email/password_reset.html`
- Create: `templates/email/password_reset.txt`
- Create: `templates/email/login_new_device.html`
- Create: `templates/email/login_new_device.txt`

- [ ] **Step 1: `2fa_code.txt`**

```
Your Daily Life Distributor login code is:

  {{ code }}

This code expires in 10 minutes. If you didn't request it, you can ignore this email.

— Daily Life Distributor
```

- [ ] **Step 2: `2fa_code.html`**

```html
<!doctype html>
<html><body>
<p>Hi {{ username }},</p>
<p>Your Daily Life Distributor login code:</p>
<p style="font-size:24px;font-family:monospace"><b>{{ code }}</b></p>
<p>This code expires in 10 minutes. If you didn't request it, ignore this email.</p>
<p>— Daily Life Distributor</p>
</body></html>
```

- [ ] **Step 3: `recovery_request.txt` (to Owners)**

```
A member of your organization has requested account recovery.

Username: {{ requester_username }}
Email:    {{ requester_email }}
Note:     {{ note }}

If this looks legitimate, approve here (expires in 48 hours):
{{ approve_url }}

— Daily Life Distributor
```

- [ ] **Step 4: `recovery_request.html`**

```html
<!doctype html>
<html><body>
<p>A member of your organization has requested account recovery.</p>
<ul>
  <li><b>Username:</b> {{ requester_username }}</li>
  <li><b>Email:</b> {{ requester_email }}</li>
  <li><b>Note:</b> {{ note }}</li>
</ul>
<p><a href="{{ approve_url }}">Approve recovery</a> (expires in 48 hours)</p>
</body></html>
```

- [ ] **Step 5: `recovery_approved.txt`**

```
Good news — your recovery request was approved by {{ approver_username }}.

Set a new password here (expires in 1 hour):
{{ reset_url }}

After resetting, you'll need to set up 2FA again.

— Daily Life Distributor
```

- [ ] **Step 6: `recovery_approved.html`**

```html
<!doctype html>
<html><body>
<p>Your recovery request was approved by {{ approver_username }}.</p>
<p><a href="{{ reset_url }}">Set a new password</a> (link expires in 1 hour).</p>
<p>After resetting, you'll need to set up 2FA again.</p>
</body></html>
```

- [ ] **Step 7: `password_reset.txt`**

```
Reset your Daily Life Distributor password by clicking the link below (expires in 1 hour):

{{ reset_url }}

If you didn't request this, ignore this email — nothing will change.

— Daily Life Distributor
```

- [ ] **Step 8: `password_reset.html`**

```html
<!doctype html>
<html><body>
<p>Reset your Daily Life Distributor password:</p>
<p><a href="{{ reset_url }}">Set a new password</a> (link expires in 1 hour).</p>
<p>If you didn't request this, ignore this email.</p>
</body></html>
```

- [ ] **Step 9: `login_new_device.txt`**

```
A new sign-in to your Daily Life Distributor account was just detected.

When:        {{ when }}
IP address:  {{ ip }}
Device:      {{ ua }}

If this was you, no action is needed. If not, change your password immediately:
{{ reset_url }}

You can turn off these alerts at {{ settings_url }}.
```

- [ ] **Step 10: `login_new_device.html`**

```html
<!doctype html>
<html><body>
<p>A new sign-in was just detected:</p>
<ul>
  <li>When: {{ when }}</li>
  <li>IP: {{ ip }}</li>
  <li>Device: {{ ua }}</li>
</ul>
<p>If this wasn't you, <a href="{{ reset_url }}">change your password now</a>.</p>
<p><a href="{{ settings_url }}">Turn off these alerts</a></p>
</body></html>
```

- [ ] **Step 11: Commit**

`git add templates/email/2fa_code.* templates/email/recovery_*.* templates/email/password_reset.* templates/email/login_new_device.* && git commit -m "feat(email): 2FA, recovery, password-reset, new-device templates"`

---

## Task 19: core/audit.py — write_event

**Files:**
- Create: `core/audit.py`
- Modify: `core/db.py` (helper)
- Create: `tests/test_audit.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_audit.py
import json
from core import audit


def test_write_event_persists_row(db):
    audit.write_event(action="user.login", actor_user_id=1, org_id=2,
                      target_type="user", target_id=1,
                      metadata={"k": "v"}, ip="1.2.3.4", ua="Mozilla")
    rows = db.list_audit_events(org_id=2)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "user.login"
    assert r["actor_user_id"] == 1
    assert r["org_id"] == 2
    assert r["target_type"] == "user"
    assert r["target_id"] == 1
    assert json.loads(r["metadata"]) == {"k": "v"}
    assert r["ip"] == "1.2.3.4"
    assert r["user_agent"] == "Mozilla"


def test_write_event_handles_nulls(db):
    audit.write_event(action="system.boot")
    rows = db.list_audit_events()
    assert len(rows) == 1
    assert rows[0]["action"] == "system.boot"
    assert rows[0]["actor_user_id"] is None
    assert rows[0]["metadata"] is None
```

- [ ] **Step 2: Implement**

```python
# core/audit.py
"""Audit event writer."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from core import db as _db


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
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    meta_json = json.dumps(metadata) if metadata is not None else None
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
    )
```

Append to `core/db.py`:

```python
def insert_audit_event(*, org_id, actor_user_id, action, target_type, target_id,
                       metadata, ip, user_agent, created_at) -> int:
    with _connect() as cx:
        cur = cx.execute(
            "INSERT INTO audit_log (org_id, actor_user_id, action, target_type, "
            "target_id, metadata, ip, user_agent, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (org_id, actor_user_id, action, target_type, target_id, metadata, ip, user_agent, created_at),
        )
        return cur.lastrowid


def list_audit_events(*, org_id: int | None = None, limit: int = 100,
                      actor_user_id: int | None = None,
                      action_prefix: str | None = None,
                      since: str | None = None,
                      until: str | None = None) -> list[dict]:
    sql = "SELECT * FROM audit_log WHERE 1=1"
    args: list = []
    if org_id is not None:
        sql += " AND org_id=?"; args.append(org_id)
    if actor_user_id is not None:
        sql += " AND actor_user_id=?"; args.append(actor_user_id)
    if action_prefix:
        sql += " AND action LIKE ?"; args.append(action_prefix + "%")
    if since:
        sql += " AND created_at>=?"; args.append(since)
    if until:
        sql += " AND created_at<=?"; args.append(until)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with _connect() as cx:
        rows = cx.execute(sql, args).fetchall()
    return [dict(r) for r in rows]
```

Run: `pytest tests/test_audit.py -q`
Expected: 2 passed.

- [ ] **Step 3: Commit**

`git add core/audit.py core/db.py tests/test_audit.py && git commit -m "feat(audit): write_event persists to audit_log with metadata JSON"`

---

## Task 20: Hook audit writes into every privileged action

**Files:**
- Modify: `blueprints/auth.py`
- Modify: `blueprints/twofa.py`
- Modify: `blueprints/recovery.py` (will be created in Task 25 — defer the recovery hooks until then; here add the login/logout/password/2fa hooks now)
- Modify: `blueprints/members.py` (PR-β: invite/accept/revoke/role-change/remove)
- Modify: `blueprints/devices.py` (PR-α: pair/revoke/rename)
- Modify: `blueprints/secrets.py` (connect/disconnect)
- Modify: `core/upload_jobs.py` (upload start/complete/fail/cancel)
- Modify: `blueprints/admin.py` (org create/disable/settings_change)
- Create: `tests/test_audit_hooks.py`

- [ ] **Step 1: Failing test for login hook**

```python
# tests/test_audit_hooks.py
def test_login_writes_audit_event(client, db):
    make_user(db, username="alice", password="hunter22hunter22")
    client.post("/login", data={"username": "alice", "password": "hunter22hunter22"},
                environ_base={"REMOTE_ADDR": "9.9.9.9", "HTTP_USER_AGENT": "TestUA"})
    rows = db.list_audit_events()
    assert any(r["action"] == "user.login" and r["ip"] == "9.9.9.9" and r["user_agent"] == "TestUA"
               for r in rows)


def test_failed_login_writes_audit_event(client, db):
    make_user(db, username="alice", password="hunter22hunter22")
    client.post("/login", data={"username": "alice", "password": "wrong"})
    assert any(r["action"] == "user.login_failed" for r in db.list_audit_events())


def test_2fa_enable_disable_audit(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    client.post("/settings/2fa/enable-totp")
    # ... verify
    actions = [r["action"] for r in db.list_audit_events()]
    assert "user.2fa_enabled" in actions or "user.2fa_setup_started" in actions
```

- [ ] **Step 2: Add helper + hooks in `blueprints/auth.py`**

```python
from core import audit as _audit


def _req_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "")


def _req_ua() -> str:
    return request.headers.get("User-Agent", "")
```

After successful password-only login (no 2FA path):

```python
_audit.write_event(action="user.login", actor_user_id=user["id"],
                   ip=_req_ip(), ua=_req_ua())
```

After failed password verify:

```python
_audit.write_event(action="user.login_failed",
                   metadata={"username": username},
                   ip=_req_ip(), ua=_req_ua())
```

After `/logout`:

```python
_audit.write_event(action="user.logout", actor_user_id=session.get("user_id"),
                   ip=_req_ip(), ua=_req_ua())
```

After successful `/login/2fa` and `/login/email-2fa`:

```python
_audit.write_event(action="user.login", actor_user_id=uid,
                   metadata={"second_factor": "totp" if used_totp else "email"
                             if used_email else "recovery_code"},
                   ip=_req_ip(), ua=_req_ua())
```

- [ ] **Step 3: Hooks in `blueprints/twofa.py`**

In `verify_totp_post()` after `_db.set_user_totp(...)`:

```python
_audit.write_event(action="user.2fa_enabled", actor_user_id=user["id"],
                   metadata={"method": "totp"}, ip=_req_ip(), ua=_req_ua())
```

In `enable_email_2fa()` after flip:

```python
_audit.write_event(action="user.2fa_enabled", actor_user_id=user["id"],
                   metadata={"method": "email"}, ip=_req_ip(), ua=_req_ua())
```

In `disable_2fa()` after flip:

```python
_audit.write_event(action="user.2fa_disabled", actor_user_id=user["id"],
                   metadata={"method": method}, ip=_req_ip(), ua=_req_ua())
```

(Reuse the `_req_ip`/`_req_ua` helpers — move them to a shared `blueprints/_common.py` for DRY: `from blueprints._common import req_ip, req_ua`.)

- [ ] **Step 4: Hooks in members/devices/secrets/upload_jobs/admin**

Pattern per file — call `_audit.write_event(...)` at every state transition. Concretely, in:

`blueprints/members.py` (from PR-β):

```python
# After creating an invite
_audit.write_event(action="invite.sent", actor_user_id=inviter_id, org_id=org_id,
                   target_type="invite", target_id=inv_id,
                   metadata={"email": email, "role": role})
# After revoke
_audit.write_event(action="invite.revoked", actor_user_id=current_user_id, org_id=org_id,
                   target_type="invite", target_id=inv_id)
# After accept (in /invite/accept handler):
_audit.write_event(action="invite.accepted", actor_user_id=new_user_id, org_id=org_id,
                   target_type="invite", target_id=inv_id)
_audit.write_event(action="org.member_added", actor_user_id=new_user_id, org_id=org_id,
                   target_type="user", target_id=new_user_id, metadata={"role": role})
# Member remove / role change
_audit.write_event(action="org.member_removed", actor_user_id=current_user_id, org_id=org_id,
                   target_type="user", target_id=removed_user_id)
_audit.write_event(action="org.role_changed", actor_user_id=current_user_id, org_id=org_id,
                   target_type="user", target_id=user_id,
                   metadata={"from": old_role, "to": new_role})
```

`blueprints/devices.py`:

```python
_audit.write_event(action="device.paired", actor_user_id=user_id, org_id=org_id,
                   target_type="device", target_id=device_id,
                   metadata={"name": device_name})
_audit.write_event(action="device.revoked", actor_user_id=user_id, org_id=org_id,
                   target_type="device", target_id=device_id)
_audit.write_event(action="device.renamed", actor_user_id=user_id, org_id=org_id,
                   target_type="device", target_id=device_id,
                   metadata={"new_name": new_name})
_audit.write_event(action="device.relinked", actor_user_id=user_id, org_id=org_id,
                   target_type="device", target_id=device_id)
```

`blueprints/secrets.py`:

```python
_audit.write_event(action="secret.connected", actor_user_id=user_id, org_id=org_id,
                   target_type="secret", metadata={"key": key})
_audit.write_event(action="secret.disconnected", actor_user_id=user_id, org_id=org_id,
                   target_type="secret", metadata={"key": key})
```

`core/upload_jobs.py` — at job dispatch, completion, failure, cancel:

```python
_audit.write_event(action="upload.started", actor_user_id=user_id, org_id=org_id,
                   target_type="upload", target_id=job_id,
                   metadata={"platforms": list(platforms), "date_count": len(dates)})
_audit.write_event(action="upload.completed", actor_user_id=user_id, org_id=org_id,
                   target_type="upload", target_id=job_id,
                   metadata={"ok": ok_count, "failed": fail_count, "skipped": skip_count})
# On exception path:
_audit.write_event(action="upload.failed", actor_user_id=user_id, org_id=org_id,
                   target_type="upload", target_id=job_id,
                   metadata={"error": str(exc)[:500]})
# On user cancel:
_audit.write_event(action="upload.cancelled", actor_user_id=user_id, org_id=org_id,
                   target_type="upload", target_id=job_id)
```

`blueprints/admin.py`:

```python
_audit.write_event(action="org.created", actor_user_id=current_user_id, org_id=new_org_id,
                   target_type="org", target_id=new_org_id,
                   metadata={"name": name, "slug": slug})
_audit.write_event(action="org.disabled", actor_user_id=current_user_id, org_id=org_id,
                   target_type="org", target_id=org_id)
_audit.write_event(action="org.settings_changed", actor_user_id=current_user_id, org_id=org_id,
                   target_type="org", target_id=org_id, metadata={"changes": diff})
```

Run: `pytest tests/test_audit_hooks.py tests/test_login_2fa.py tests/test_twofa_routes.py -q`
Expected: passes.

- [ ] **Step 5: Commit**

`git add blueprints/auth.py blueprints/twofa.py blueprints/members.py blueprints/devices.py blueprints/secrets.py core/upload_jobs.py blueprints/admin.py tests/test_audit_hooks.py && git commit -m "feat(audit): emit events from login/2fa/invite/member/device/secret/upload/org actions"`

---

## Task 21: GET /settings/audit-log — org-scoped, role-gated

**Files:**
- Create: `blueprints/audit.py`
- Create: `templates/audit_log.html`
- Modify: `app.py` (register)
- Create: `tests/test_audit_log_route.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_audit_log_route.py
def test_user_role_forbidden(client, db):
    org = make_org(db, "Acme")
    user = make_user(db, username="u")
    add_membership(db, user["id"], org["id"], role="user")
    login_as(client, user, current_org_id=org["id"])
    resp = client.get("/settings/audit-log")
    assert resp.status_code == 403


def test_owner_sees_org_scoped_events_only(client, db):
    org_a = make_org(db, "Acme")
    org_b = make_org(db, "Beta")
    owner = make_user(db, username="o")
    add_membership(db, owner["id"], org_a["id"], role="owner")
    audit.write_event(action="upload.started", actor_user_id=99, org_id=org_a["id"])
    audit.write_event(action="upload.started", actor_user_id=99, org_id=org_b["id"])
    login_as(client, owner, current_org_id=org_a["id"])
    resp = client.get("/settings/audit-log")
    body = resp.get_data(as_text=True)
    assert "Acme" not in body or "upload.started" in body
    # Only one row visible
    assert body.count("upload.started") == 1


def test_filters_by_action_and_date(client, db):
    org = make_org(db, "Acme")
    owner = make_user(db, username="o")
    add_membership(db, owner["id"], org["id"], role="owner")
    audit.write_event(action="user.login", actor_user_id=owner["id"], org_id=org["id"])
    audit.write_event(action="upload.failed", actor_user_id=owner["id"], org_id=org["id"])
    login_as(client, owner, current_org_id=org["id"])
    r = client.get("/settings/audit-log?action=upload.")
    body = r.get_data(as_text=True)
    assert "upload.failed" in body
    assert "user.login" not in body
```

- [ ] **Step 2: Blueprint**

```python
# blueprints/audit.py
from __future__ import annotations

from flask import Blueprint, render_template, request, session, abort

from blueprints.auth import login_required
from core import db as _db

bp = Blueprint("audit", __name__)


def _user_role_in_org(user_id: int, org_id: int) -> str | None:
    m = _db.get_membership(user_id, org_id)
    return m["role"] if m else None


@bp.get("/settings/audit-log")
@login_required
def audit_log_view():
    uid = session.get("user_id")
    org_id = session.get("current_org_id")
    if not org_id:
        abort(400)
    role = _user_role_in_org(uid, org_id)
    if role not in ("owner", "manager"):
        abort(403)
    action_prefix = request.args.get("action") or None
    actor = request.args.get("actor")
    actor_id = int(actor) if actor and actor.isdigit() else None
    since = request.args.get("since") or None
    until = request.args.get("until") or None
    rows = _db.list_audit_events(
        org_id=org_id, action_prefix=action_prefix, actor_user_id=actor_id,
        since=since, until=until, limit=500,
    )
    return render_template("audit_log.html", rows=rows,
                           filters={"action": action_prefix, "actor": actor_id,
                                    "since": since, "until": until})
```

- [ ] **Step 3: Register in `app.py`**

```python
from blueprints.audit import bp as audit_bp
app.register_blueprint(audit_bp)
```

Run: `pytest tests/test_audit_log_route.py -q`
Expected: passes.

- [ ] **Step 4: Commit**

`git add blueprints/audit.py app.py tests/test_audit_log_route.py && git commit -m "feat(audit): /settings/audit-log org-scoped + role-gated"`

---

## Task 22: templates/audit_log.html

**Files:**
- Create: `templates/audit_log.html`

- [ ] **Step 1: Template**

```html
{% extends "base.html" %}
{% block content %}
<h1>Audit log</h1>

<form method="get" class="filters">
  <label>Action prefix: <input name="action" value="{{ filters.action or '' }}" placeholder="upload."></label>
  <label>Actor ID: <input name="actor" value="{{ filters.actor or '' }}"></label>
  <label>Since: <input type="date" name="since" value="{{ filters.since or '' }}"></label>
  <label>Until: <input type="date" name="until" value="{{ filters.until or '' }}"></label>
  <button type="submit">Filter</button>
</form>

<table>
  <thead>
    <tr>
      <th>When</th>
      <th>Action</th>
      <th>Actor</th>
      <th>Target</th>
      <th>Metadata</th>
      <th>IP</th>
    </tr>
  </thead>
  <tbody>
    {% for r in rows %}
    <tr>
      <td>{{ r.created_at }}</td>
      <td><code>{{ r.action }}</code></td>
      <td>{{ r.actor_user_id }}</td>
      <td>{{ r.target_type }}{% if r.target_id %} #{{ r.target_id }}{% endif %}</td>
      <td>{% if r.metadata %}<pre>{{ r.metadata }}</pre>{% endif %}</td>
      <td>{{ r.ip }}</td>
    </tr>
    {% else %}
    <tr><td colspan="6"><em>No events.</em></td></tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
```

Run: `pytest tests/test_audit_log_route.py -q`
Expected: still passing.

- [ ] **Step 2: Commit**

`git add templates/audit_log.html && git commit -m "feat(audit): audit_log.html with filters"`

---

## Task 23: /admin/audit-log cross-org search

**Files:**
- Modify: `blueprints/admin.py`
- Create: `tests/test_admin_audit_log.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_admin_audit_log.py
def test_program_owner_sees_all_orgs(client, db):
    a = make_org(db, "Acme"); b = make_org(db, "Beta")
    audit.write_event(action="user.login", actor_user_id=1, org_id=a["id"])
    audit.write_event(action="user.login", actor_user_id=2, org_id=b["id"])
    po = make_user(db, username="po", program_owner=True)
    login_as(client, po)
    resp = client.get("/admin/audit-log")
    body = resp.get_data(as_text=True)
    assert body.count("user.login") == 2


def test_non_program_owner_forbidden(client, db):
    user = make_user(db, username="alice")
    login_as(client, user)
    resp = client.get("/admin/audit-log")
    assert resp.status_code == 403
```

- [ ] **Step 2: Add route to `blueprints/admin.py`**

```python
@bp.get("/admin/audit-log")
@login_required
def admin_audit_log():
    user = _db.get_user_by_id(session.get("user_id"))
    if not user or not user.get("program_owner"):
        abort(403)
    action_prefix = request.args.get("action") or None
    org_id = request.args.get("org_id")
    org_id_int = int(org_id) if org_id and org_id.isdigit() else None
    since = request.args.get("since") or None
    until = request.args.get("until") or None
    rows = _db.list_audit_events(
        org_id=org_id_int, action_prefix=action_prefix,
        since=since, until=until, limit=1000,
    )
    return render_template("audit_log.html", rows=rows,
                           filters={"action": action_prefix, "actor": None,
                                    "since": since, "until": until},
                           cross_org=True)
```

Run: `pytest tests/test_admin_audit_log.py -q`
Expected: passes.

- [ ] **Step 3: Commit**

`git add blueprints/admin.py tests/test_admin_audit_log.py && git commit -m "feat(admin): /admin/audit-log cross-org search for program-owner"`

---

## Task 24: core/recovery_request.py — submit + rate-limit

**Files:**
- Create: `core/recovery_request.py`
- Modify: `core/db.py`
- Create: `tests/test_recovery_request.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_recovery_request.py
from datetime import timedelta
from freezegun import freeze_time

from core import recovery_request


def test_submit_creates_row_and_emails_all_owners(db, captured_emails):
    org = make_org(db, "Acme")
    user = make_user(db, username="alice", email="alice@example.com")
    add_membership(db, user["id"], org["id"], role="user")
    o1 = make_user(db, username="o1", email="o1@x.com")
    add_membership(db, o1["id"], org["id"], role="owner")
    o2 = make_user(db, username="o2", email="o2@x.com")
    add_membership(db, o2["id"], org["id"], role="owner")
    rid = recovery_request.submit_request("alice", note="lost my phone")
    assert rid > 0
    targets = [m["to"] for m in captured_emails if m["template"] == "recovery_request"]
    assert "o1@x.com" in targets and "o2@x.com" in targets


def test_unknown_username_silently_succeeds(db, captured_emails):
    # No user-enumeration: return a fake id; no email sent
    rid = recovery_request.submit_request("ghost", note="hi")
    assert rid == 0 or rid is None or isinstance(rid, int)
    assert not any(m["template"] == "recovery_request" for m in captured_emails)


def test_rate_limit_one_per_24h(db, captured_emails):
    org = make_org(db, "Acme")
    user = make_user(db, username="alice")
    add_membership(db, user["id"], org["id"], role="user")
    with freeze_time("2026-05-23 10:00:00"):
        r1 = recovery_request.submit_request("alice", note="first")
        assert r1 > 0
    with freeze_time("2026-05-23 22:00:00"):  # 12h later — still in window
        r2 = recovery_request.submit_request("alice", note="second")
        assert r2 is None
    with freeze_time("2026-05-24 11:00:00"):  # >24h
        r3 = recovery_request.submit_request("alice", note="later")
        assert r3 > 0
```

- [ ] **Step 2: Implement**

```python
# core/recovery_request.py
"""Admin-approved out-of-band recovery requests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import url_for, current_app

from core import db as _db
from core import email as _email
from core import audit as _audit

_REQ_TTL = timedelta(hours=48)
_RATE_WINDOW = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def submit_request(username: str, note: str) -> int | None:
    user = _db.get_user_by_username(username)
    if not user:
        return None  # no enumeration
    recent_iso = (_now() - _RATE_WINDOW).isoformat()
    if _db.count_recovery_requests_since(user["id"], recent_iso) > 0:
        return None  # rate-limited
    now = _now()
    rid = _db.insert_recovery_request(
        user_id=user["id"],
        requested_at=now.isoformat(),
        expires_at=(now + _REQ_TTL).isoformat(),
        note=note,
    )
    # Find all Owners across all orgs the user belongs to
    owners = _db.list_org_owners_for_user(user["id"])
    for o in owners:
        approve_url = current_app.config.get("BASE_URL", "https://autoalert.pro") \
            + url_for("recovery.approve", request_id=rid)
        _email.send("recovery_request",
                    to=o["email"],
                    requester_username=user["username"],
                    requester_email=user["email"],
                    note=note,
                    approve_url=approve_url)
    _audit.write_event(action="user.recovery_requested",
                       actor_user_id=user["id"],
                       target_type="user", target_id=user["id"],
                       metadata={"note": note[:200]})
    return rid
```

Append to `core/db.py`:

```python
def insert_recovery_request(*, user_id: int, requested_at: str, expires_at: str, note: str) -> int:
    with _connect() as cx:
        cur = cx.execute(
            "INSERT INTO recovery_requests (user_id, requested_at, expires_at, note) "
            "VALUES (?,?,?,?)",
            (user_id, requested_at, expires_at, note),
        )
        return cur.lastrowid


def count_recovery_requests_since(user_id: int, since_iso: str) -> int:
    with _connect() as cx:
        row = cx.execute(
            "SELECT COUNT(*) AS c FROM recovery_requests WHERE user_id=? AND requested_at>=?",
            (user_id, since_iso),
        ).fetchone()
    return int(row["c"])


def list_org_owners_for_user(user_id: int) -> list[dict]:
    with _connect() as cx:
        rows = cx.execute(
            "SELECT DISTINCT u.* FROM users u "
            "JOIN org_memberships om2 ON om2.user_id = u.id AND om2.role='owner' "
            "WHERE om2.org_id IN ("
            "  SELECT org_id FROM org_memberships WHERE user_id=?"
            ")",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]
```

(Add `note TEXT` column to `recovery_requests` schema in `init_db()` if not already present.)

Run: `pytest tests/test_recovery_request.py -q`
Expected: passes.

- [ ] **Step 3: Commit**

`git add core/recovery_request.py core/db.py tests/test_recovery_request.py && git commit -m "feat(recovery): submit_request + 24h rate-limit + Owner emails"`

---

## Task 25: /recover + /admin-actions/recovery/<id>/approve

**Files:**
- Create: `blueprints/recovery.py`
- Create: `templates/recover.html`
- Modify: `app.py`
- Create: `tests/test_recovery_flow.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_recovery_flow.py
from itsdangerous import URLSafeTimedSerializer


def test_post_recover_creates_request(client, db, captured_emails):
    org = make_org(db, "Acme")
    user = make_user(db, username="alice", email="alice@example.com")
    add_membership(db, user["id"], org["id"], role="user")
    owner = make_user(db, username="o", email="o@x.com")
    add_membership(db, owner["id"], org["id"], role="owner")
    resp = client.post("/recover", data={"username": "alice", "note": "lost phone"})
    assert resp.status_code in (200, 302)
    assert any(m["template"] == "recovery_request" and "o@x.com" in m["to"]
               for m in captured_emails)


def test_owner_clicks_approve_link_emails_reset(client, db, captured_emails, app):
    org = make_org(db, "Acme")
    user = make_user(db, username="alice", email="alice@example.com")
    add_membership(db, user["id"], org["id"], role="user")
    owner = make_user(db, username="o", email="o@x.com")
    add_membership(db, owner["id"], org["id"], role="owner")
    client.post("/recover", data={"username": "alice", "note": "lost phone"})
    rid = db.list_recovery_requests()[0]["id"]
    login_as(client, owner)
    resp = client.get(f"/admin-actions/recovery/{rid}/approve")
    assert resp.status_code in (200, 302)
    reset = next(m for m in captured_emails if m["template"] == "recovery_approved")
    assert "alice@example.com" in reset["to"]
    assert "reset_url" in reset["vars"]


def test_non_owner_cannot_approve(client, db):
    org = make_org(db, "Acme")
    user = make_user(db, username="alice", email="alice@example.com")
    add_membership(db, user["id"], org["id"], role="user")
    other = make_user(db, username="other", email="other@x.com")
    # other is NOT an owner of Acme
    client.post("/recover", data={"username": "alice", "note": "x"})
    rid = db.list_recovery_requests()[0]["id"]
    login_as(client, other)
    resp = client.get(f"/admin-actions/recovery/{rid}/approve")
    assert resp.status_code == 403
```

- [ ] **Step 2: Blueprint**

```python
# blueprints/recovery.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template, request, redirect, url_for, session, abort, current_app
from itsdangerous import URLSafeTimedSerializer

from blueprints.auth import login_required
from core import db as _db
from core import recovery_request as _rreq
from core import recovery as _recovery
from core import email as _email
from core import audit as _audit

bp = Blueprint("recovery", __name__)


@bp.get("/recover")
def recover_form():
    return render_template("recover.html")


@bp.post("/recover")
def recover_submit():
    username = (request.form.get("username") or "").strip()
    note = (request.form.get("note") or "").strip()[:1000]
    _rreq.submit_request(username, note)
    # Always show generic message — no enumeration
    return render_template("recover.html",
                           message="If this account exists, your organization's owners have been notified.")


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="recovery-reset")


@bp.get("/admin-actions/recovery/<int:request_id>/approve")
@login_required
def approve(request_id: int):
    rrow = _db.get_recovery_request(request_id)
    if not rrow:
        abort(404)
    if rrow.get("approved_at") or rrow.get("consumed_at"):
        return render_template("recover.html", message="This request has already been processed.")
    # Owner check: approver must own at least one of the requester's orgs
    approver_id = session.get("user_id")
    if not _db.user_owns_any_org_with(approver_id, rrow["user_id"]):
        abort(403)
    now = datetime.now(timezone.utc).isoformat()
    token = _reset_serializer().dumps({"uid": rrow["user_id"], "rid": request_id})
    _db.update_recovery_request_approve(request_id, approver_id, now, token)
    requester = _db.get_user_by_id(rrow["user_id"])
    reset_url = current_app.config.get("BASE_URL", "https://autoalert.pro") \
        + url_for("recovery.reset_form") + f"?token={token}"
    approver = _db.get_user_by_id(approver_id)
    _email.send("recovery_approved",
                to=requester["email"],
                approver_username=approver["username"],
                reset_url=reset_url)
    _audit.write_event(action="user.recovery_approved",
                       actor_user_id=approver_id,
                       target_type="user", target_id=requester["id"],
                       metadata={"request_id": request_id})
    return render_template("recover.html",
                           message=f"Recovery approved. An email was sent to {requester['email']}.")
```

Add db helpers:

```python
def get_recovery_request(rid: int) -> dict | None:
    with _connect() as cx:
        row = cx.execute("SELECT * FROM recovery_requests WHERE id=?", (rid,)).fetchone()
    return dict(row) if row else None


def list_recovery_requests() -> list[dict]:
    with _connect() as cx:
        rows = cx.execute("SELECT * FROM recovery_requests ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def update_recovery_request_approve(rid: int, approver_user_id: int, approved_at: str, token: str) -> None:
    import hashlib
    h = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _connect() as cx:
        cx.execute(
            "UPDATE recovery_requests SET approver_user_id=?, approved_at=?, password_reset_token_hash=? "
            "WHERE id=?",
            (approver_user_id, approved_at, h, rid),
        )


def user_owns_any_org_with(approver_id: int, target_user_id: int) -> bool:
    with _connect() as cx:
        row = cx.execute(
            "SELECT 1 FROM org_memberships a "
            "JOIN org_memberships b ON a.org_id = b.org_id "
            "WHERE a.user_id=? AND a.role='owner' AND b.user_id=? LIMIT 1",
            (approver_id, target_user_id),
        ).fetchone()
    return row is not None
```

Add `templates/recover.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Account recovery</h1>
{% if message %}<p class="success">{{ message }}</p>{% endif %}
<p>If you've lost your password, your 2FA device, AND your recovery codes, submit this form. An owner of your organization will be emailed; once they approve, you'll get a password-reset link.</p>
<form method="post" action="/recover">
  <label>Username: <input name="username" required></label>
  <label>What happened? <textarea name="note" rows="4" required></textarea></label>
  <button type="submit">Submit recovery request</button>
</form>
{% endblock %}
```

Register in `app.py`:

```python
from blueprints.recovery import bp as recovery_bp
app.register_blueprint(recovery_bp)
```

Run: `pytest tests/test_recovery_flow.py -q`
Expected: passes.

- [ ] **Step 3: Commit**

`git add blueprints/recovery.py templates/recover.html core/db.py app.py tests/test_recovery_flow.py && git commit -m "feat(recovery): /recover submit + /admin-actions/recovery/<id>/approve"`

---

## Task 26: /recover/reset?token=... — set new password, clear 2FA

**Files:**
- Modify: `blueprints/recovery.py`
- Create: `templates/recover_reset.html`
- Modify: `tests/test_recovery_flow.py`

- [ ] **Step 1: Failing test**

```python
def test_reset_with_valid_token_sets_password_and_clears_totp(client, db, app):
    org = make_org(db, "Acme")
    user = make_user(db, username="alice", email="a@x.com",
                     totp_enabled=True, totp_secret_encrypted="enc")
    add_membership(db, user["id"], org["id"], role="user")
    owner = make_user(db, username="o", email="o@x.com")
    add_membership(db, owner["id"], org["id"], role="owner")
    client.post("/recover", data={"username": "alice", "note": "x"})
    rid = db.list_recovery_requests()[0]["id"]
    login_as(client, owner)
    client.get(f"/admin-actions/recovery/{rid}/approve")
    # Pull token off the email
    from tests.helpers import last_email
    msg = last_email(template="recovery_approved")
    token = msg["vars"]["reset_url"].split("token=")[1]
    client.get("/logout")  # clear owner session
    r = client.post(f"/recover/reset?token={token}",
                    data={"password": "newhunter22hunter", "password2": "newhunter22hunter"})
    assert r.status_code in (200, 302)
    row = db.get_user_by_id(user["id"])
    # New password works
    from core import auth as _auth
    assert _auth.verify_password_for_user(user["id"], "newhunter22hunter") is True
    # TOTP cleared
    assert row["totp_enabled"] == 0
    assert row["totp_secret_encrypted"] is None
    # Recovery codes invalidated
    assert db.list_recovery_codes(user["id"]) == []
```

- [ ] **Step 2: Reset routes**

Append to `blueprints/recovery.py`:

```python
import hashlib

from core import auth as _auth


@bp.get("/recover/reset")
def reset_form():
    return render_template("recover_reset.html", token=request.args.get("token", ""))


@bp.post("/recover/reset")
def reset_submit():
    token = request.values.get("token", "")
    pw = request.form.get("password", "")
    pw2 = request.form.get("password2", "")
    if pw != pw2 or len(pw) < 12:
        return render_template("recover_reset.html", token=token,
                               error="Passwords must match and be at least 12 chars."), 400
    try:
        data = _reset_serializer().loads(token, max_age=3600)  # 1 hour
    except Exception:
        return render_template("recover_reset.html", token=token,
                               error="Token expired or invalid."), 400
    rid = data["rid"]; uid = data["uid"]
    rrow = _db.get_recovery_request(rid)
    if not rrow or rrow.get("consumed_at"):
        return render_template("recover_reset.html", token=token,
                               error="Token already used."), 400
    h = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if rrow.get("password_reset_token_hash") != h:
        abort(400)
    _auth.set_user_password(uid, pw)
    _db.set_user_totp(uid, None, enabled=False)
    _db.set_user_email_2fa(uid, False)
    _db.delete_recovery_codes(uid)
    _db.consume_recovery_request(rid)
    _audit.write_event(action="user.password_changed", actor_user_id=uid,
                       metadata={"via": "recovery"})
    return redirect(url_for("auth.login"))
```

Add db helper:

```python
def consume_recovery_request(rid: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as cx:
        cx.execute("UPDATE recovery_requests SET consumed_at=? WHERE id=?", (now, rid))
```

Add `templates/recover_reset.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Set a new password</h1>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post" action="/recover/reset">
  <input type="hidden" name="token" value="{{ token }}">
  <label>New password (≥12 chars): <input type="password" name="password" minlength="12" required></label>
  <label>Confirm: <input type="password" name="password2" minlength="12" required></label>
  <button type="submit">Set password</button>
</form>
{% endblock %}
```

Run: `pytest tests/test_recovery_flow.py -q`
Expected: passes.

- [ ] **Step 3: Commit**

`git add blueprints/recovery.py templates/recover_reset.html core/db.py tests/test_recovery_flow.py && git commit -m "feat(recovery): /recover/reset sets password, clears 2FA + codes"`

---

## Task 27: Login-from-new-device notifications

**Files:**
- Create: `core/login_notifications.py`
- Modify: `blueprints/auth.py` (call from login success)
- Modify: `core/db.py` (table)
- Create: `tests/test_login_notifications.py`

- [ ] **Step 1: Schema for login-IP sightings**

In `core/db.py` `init_db()`:

```python
cx.executescript("""
CREATE TABLE IF NOT EXISTS login_ip_sightings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    ip TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE(user_id, ip)
);
""")
```

Helpers:

```python
def get_login_ip_sighting(user_id: int, ip: str) -> dict | None:
    with _connect() as cx:
        row = cx.execute(
            "SELECT * FROM login_ip_sightings WHERE user_id=? AND ip=?",
            (user_id, ip),
        ).fetchone()
    return dict(row) if row else None


def upsert_login_ip_sighting(user_id: int, ip: str, now_iso: str) -> bool:
    """Returns True iff this is a first sighting (insert), False if updated."""
    with _connect() as cx:
        cur = cx.execute(
            "INSERT INTO login_ip_sightings (user_id, ip, first_seen, last_seen) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, ip) DO UPDATE SET last_seen=excluded.last_seen",
            (user_id, ip, now_iso, now_iso),
        )
        return cur.rowcount == 1 and cx.execute(
            "SELECT first_seen=last_seen AS f FROM login_ip_sightings WHERE user_id=? AND ip=?",
            (user_id, ip),
        ).fetchone()["f"] == 1
```

- [ ] **Step 2: Failing test**

```python
# tests/test_login_notifications.py
from core import login_notifications


def test_first_sighting_emails(db, captured_emails):
    user = make_user(db, username="alice", email="a@x.com")
    login_notifications.notify_if_new_device(user["id"], "1.2.3.4", "TestUA")
    assert any(m["template"] == "login_new_device" and "a@x.com" in m["to"]
               for m in captured_emails)


def test_second_sighting_silent(db, captured_emails):
    user = make_user(db, username="alice", email="a@x.com")
    login_notifications.notify_if_new_device(user["id"], "1.2.3.4", "TestUA")
    captured_emails.clear()
    login_notifications.notify_if_new_device(user["id"], "1.2.3.4", "TestUA")
    assert not any(m["template"] == "login_new_device" for m in captured_emails)


def test_disabled_preference_suppresses(db, captured_emails):
    user = make_user(db, username="alice", email="a@x.com",
                     notify_new_device=False)
    login_notifications.notify_if_new_device(user["id"], "1.2.3.4", "TestUA")
    assert not any(m["template"] == "login_new_device" for m in captured_emails)
```

- [ ] **Step 3: Implement**

```python
# core/login_notifications.py
"""Email user on first sighting of (user_id, ip) within last 30 days."""
from __future__ import annotations

from datetime import datetime, timezone

from flask import url_for, current_app

from core import db as _db
from core import email as _email


def notify_if_new_device(user_id: int, ip: str, ua: str) -> None:
    user = _db.get_user_by_id(user_id)
    if not user:
        return
    if not user.get("notify_new_device", True):
        return
    now = datetime.now(timezone.utc)
    existing = _db.get_login_ip_sighting(user_id, ip)
    _db.upsert_login_ip_sighting(user_id, ip, now.isoformat())
    if existing:
        return
    base = current_app.config.get("BASE_URL", "https://autoalert.pro")
    _email.send("login_new_device",
                to=user["email"],
                when=now.isoformat(),
                ip=ip,
                ua=ua,
                reset_url=base + url_for("auth.login"),
                settings_url=base + "/settings/security")
```

Add `users.notify_new_device BOOLEAN NOT NULL DEFAULT 1` column in `init_db()` (with idempotent ALTER if existing).

- [ ] **Step 4: Wire into auth.py**

In `login_submit()` after a successful no-2FA login, and in `login_2fa_post()` + `login_email_2fa_post()` after finalize:

```python
from core import login_notifications as _login_notif
_login_notif.notify_if_new_device(user["id"], _req_ip(), _req_ua())
```

Run: `pytest tests/test_login_notifications.py -q`
Expected: passes.

- [ ] **Step 5: Commit**

`git add core/login_notifications.py core/db.py blueprints/auth.py tests/test_login_notifications.py && git commit -m "feat(auth): email on first sighting of (user, ip)"`

---

## Task 28: /settings/security — preferences

**Files:**
- Modify: `blueprints/settings.py`
- Create: `templates/settings_security.html`
- Create: `tests/test_settings_security.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_settings_security.py
def test_get_settings_security_shows_pref(client, db):
    user = make_user(db, username="alice", notify_new_device=True)
    login_as(client, user)
    resp = client.get("/settings/security")
    body = resp.get_data(as_text=True)
    assert "Email me on new device sign-ins" in body
    assert "checked" in body


def test_post_disables_pref(client, db):
    user = make_user(db, username="alice", notify_new_device=True)
    login_as(client, user)
    resp = client.post("/settings/security", data={})  # checkbox unchecked
    assert resp.status_code in (200, 302)
    assert db.get_user_by_id(user["id"])["notify_new_device"] == 0
```

- [ ] **Step 2: Routes**

In `blueprints/settings.py`:

```python
from flask import Blueprint, render_template, request, redirect, url_for, session
from blueprints.auth import login_required
from core import db as _db

# bp already exists; append:

@bp.get("/settings/security")
@login_required
def security_get():
    user = _db.get_user_by_id(session.get("user_id"))
    return render_template("settings_security.html",
                           notify_new_device=bool(user.get("notify_new_device", True)))


@bp.post("/settings/security")
@login_required
def security_post():
    uid = session.get("user_id")
    on = bool(request.form.get("notify_new_device"))
    _db.set_user_notify_new_device(uid, on)
    return redirect(url_for("settings.security_get"))
```

Append db helper:

```python
def set_user_notify_new_device(user_id: int, enabled: bool) -> None:
    with _connect() as cx:
        cx.execute("UPDATE users SET notify_new_device=? WHERE id=?", (1 if enabled else 0, user_id))
```

Template:

```html
{# templates/settings_security.html #}
{% extends "base.html" %}
{% block content %}
<h1>Security preferences</h1>
<form method="post" action="/settings/security">
  <label>
    <input type="checkbox" name="notify_new_device" value="1" {% if notify_new_device %}checked{% endif %}>
    Email me on new device sign-ins
  </label>
  <button type="submit">Save</button>
</form>
{% endblock %}
```

Run: `pytest tests/test_settings_security.py -q`
Expected: passes.

- [ ] **Step 3: Commit**

`git add blueprints/settings.py templates/settings_security.html core/db.py tests/test_settings_security.py && git commit -m "feat(settings): /settings/security toggle for new-device emails"`

---

## Task 29: Org-level Require 2FA toggle + enforcement

**Files:**
- Modify: `blueprints/settings.py`
- Modify: `app.py` (before_request enforcement)
- Create: `tests/test_require_2fa.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_require_2fa.py
def test_only_owner_can_flip_require_2fa(client, db):
    org = make_org(db, "Acme")
    mgr = make_user(db, username="m")
    add_membership(db, mgr["id"], org["id"], role="manager")
    login_as(client, mgr, current_org_id=org["id"])
    resp = client.post("/settings/org/require-2fa", data={"enabled": "1"})
    assert resp.status_code == 403
    assert db.get_org(org["id"])["require_2fa"] == 0


def test_owner_flips_require_2fa_writes_audit(client, db):
    org = make_org(db, "Acme")
    owner = make_user(db, username="o")
    add_membership(db, owner["id"], org["id"], role="owner")
    login_as(client, owner, current_org_id=org["id"])
    resp = client.post("/settings/org/require-2fa", data={"enabled": "1"})
    assert resp.status_code in (200, 302)
    assert db.get_org(org["id"])["require_2fa"] == 1
    actions = [r["action"] for r in db.list_audit_events(org_id=org["id"])]
    assert "org.settings_changed" in actions


def test_user_without_2fa_redirected_when_org_requires_it(client, db):
    org = make_org(db, "Acme", require_2fa=True)
    user = make_user(db, username="u", totp_enabled=False, email_2fa_enabled=False)
    add_membership(db, user["id"], org["id"], role="user")
    login_as(client, user, current_org_id=org["id"])
    resp = client.get("/")  # dashboard / upload route
    assert resp.status_code == 302
    assert "/settings/2fa" in resp.headers["Location"]


def test_user_with_totp_passes_enforcement(client, db):
    org = make_org(db, "Acme", require_2fa=True)
    user = make_user(db, username="u", totp_enabled=True)
    add_membership(db, user["id"], org["id"], role="user")
    login_as(client, user, current_org_id=org["id"])
    resp = client.get("/")
    assert resp.status_code == 200
```

- [ ] **Step 2: Toggle route**

In `blueprints/settings.py`:

```python
from core import audit as _audit


@bp.post("/settings/org/require-2fa")
@login_required
def org_require_2fa():
    uid = session.get("user_id")
    org_id = session.get("current_org_id")
    if not org_id:
        return ("No org selected", 400)
    m = _db.get_membership(uid, org_id)
    if not m or m["role"] != "owner":
        return ("Forbidden", 403)
    before = _db.get_org(org_id)["require_2fa"]
    enabled = bool(request.form.get("enabled"))
    _db.set_org_require_2fa(org_id, enabled)
    _audit.write_event(action="org.settings_changed",
                       actor_user_id=uid, org_id=org_id,
                       target_type="org", target_id=org_id,
                       metadata={"changes": {"require_2fa": [bool(before), enabled]}})
    return redirect(url_for("settings.org_settings"))
```

Db helper:

```python
def set_org_require_2fa(org_id: int, enabled: bool) -> None:
    with _connect() as cx:
        cx.execute("UPDATE organizations SET require_2fa=? WHERE id=?", (1 if enabled else 0, org_id))
```

- [ ] **Step 3: Enforcement in `app.py` `before_request`**

```python
ENFORCE_PATHS = ("/", "/upload", "/media/", "/review", "/confirm")
EXEMPT_PATHS = ("/settings/2fa", "/logout", "/static", "/login")


def _enforce_2fa():
    if not session.get("user_id"):
        return
    org_id = session.get("current_org_id")
    if not org_id:
        return
    org = _db.get_org(org_id)
    if not org or not org.get("require_2fa"):
        return
    user = _db.get_user_by_id(session["user_id"])
    if user.get("totp_enabled") or user.get("email_2fa_enabled"):
        return
    p = request.path
    if any(p.startswith(e) for e in EXEMPT_PATHS):
        return
    if any(p.startswith(g) for g in ENFORCE_PATHS):
        return redirect(url_for("twofa.settings_2fa"))


app.before_request(_enforce_2fa)
```

Run: `pytest tests/test_require_2fa.py -q`
Expected: passes.

- [ ] **Step 4: Commit**

`git add blueprints/settings.py core/db.py app.py tests/test_require_2fa.py && git commit -m "feat(2fa): org Require-2FA toggle + before_request enforcement"`

---

## Task 30: core/audit_archive.py — nightly rotation

**Files:**
- Create: `core/audit_archive.py`
- Modify: `core/db.py`
- Create: `tests/test_audit_archive.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_audit_archive.py
from datetime import timedelta
from freezegun import freeze_time

from core import audit, audit_archive


def test_old_rows_moved_in_batches(db):
    with freeze_time("2025-01-01"):
        for i in range(2500):
            audit.write_event(action="upload.started", actor_user_id=i, org_id=1)
    with freeze_time("2026-05-23"):
        audit.write_event(action="user.login", actor_user_id=42, org_id=1)
        n = audit_archive.archive_old_entries(batch_size=1000)
    assert n == 2500
    # active table has only the recent one
    active = db.list_audit_events(limit=10000)
    assert len(active) == 1
    assert active[0]["action"] == "user.login"
    # archive has the old 2500
    archived = db.list_audit_archive(limit=10000)
    assert len(archived) == 2500


def test_archive_is_idempotent_when_no_old_rows(db):
    audit.write_event(action="user.login", actor_user_id=1, org_id=1)
    n = audit_archive.archive_old_entries()
    assert n == 0
```

- [ ] **Step 2: Implement**

```python
# core/audit_archive.py
"""Move audit_log rows older than 365 days into audit_log_archive."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import db as _db

_RETENTION = timedelta(days=365)


def archive_old_entries(batch_size: int = 1000) -> int:
    cutoff = (datetime.now(timezone.utc) - _RETENTION).isoformat()
    total = 0
    while True:
        moved = _db.archive_audit_batch(cutoff, batch_size)
        if moved == 0:
            break
        total += moved
    return total
```

Db helper (atomic copy-then-delete in a transaction):

```python
def archive_audit_batch(cutoff_iso: str, batch_size: int) -> int:
    with _connect() as cx:
        rows = cx.execute(
            "SELECT id FROM audit_log WHERE created_at<? ORDER BY id LIMIT ?",
            (cutoff_iso, batch_size),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        cx.execute(
            f"INSERT INTO audit_log_archive "
            f"  (id, org_id, actor_user_id, action, target_type, target_id, metadata, ip, user_agent, created_at) "
            f"SELECT id, org_id, actor_user_id, action, target_type, target_id, metadata, ip, user_agent, created_at "
            f"FROM audit_log WHERE id IN ({placeholders})",
            ids,
        )
        cx.execute(f"DELETE FROM audit_log WHERE id IN ({placeholders})", ids)
        return len(ids)


def list_audit_archive(limit: int = 100) -> list[dict]:
    with _connect() as cx:
        rows = cx.execute(
            "SELECT * FROM audit_log_archive ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
```

Run: `pytest tests/test_audit_archive.py -q`
Expected: passes.

- [ ] **Step 3: Commit**

`git add core/audit_archive.py core/db.py tests/test_audit_archive.py && git commit -m "feat(audit): batched nightly archive_old_entries(>365d)"`

---

## Task 31: APScheduler bootstrap + 03:00 UTC archive job

**Files:**
- Modify: `app.py`
- Create: `tests/test_scheduler_boot.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_scheduler_boot.py
def test_scheduler_has_audit_archive_job(app):
    sched = app.config["scheduler"]
    job_ids = {j.id for j in sched.get_jobs()}
    assert "audit_archive" in job_ids
    j = sched.get_job("audit_archive")
    assert j.trigger.fields[5].name == "hour"  # cron field
```

- [ ] **Step 2: Wire APScheduler in `app.py`**

In `create_app()`:

```python
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core.audit_archive import archive_old_entries

def _start_scheduler(app):
    if app.config.get("TESTING") and not app.config.get("SCHEDULER_FORCE"):
        # Still create the scheduler but don't start it
        sched = BackgroundScheduler(timezone="UTC")
    else:
        sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(
        archive_old_entries,
        trigger=CronTrigger(hour=3, minute=0, timezone="UTC"),
        id="audit_archive",
        replace_existing=True,
        max_instances=1,
    )
    if not app.config.get("TESTING"):
        sched.start()
    app.config["scheduler"] = sched

_start_scheduler(app)
```

Add a teardown:

```python
import atexit
atexit.register(lambda: app.config["scheduler"].shutdown(wait=False)
                if app.config.get("scheduler") and app.config["scheduler"].running else None)
```

Run: `pytest tests/test_scheduler_boot.py -q`
Expected: passes.

- [ ] **Step 3: Commit**

`git add app.py tests/test_scheduler_boot.py && git commit -m "feat(audit): boot APScheduler with nightly archive job at 03:00 UTC"`

---

## Task 32: Self-review + commit message draft + PR description

**Files:**
- No code changes. Generate review notes.

- [ ] **Step 1: Run full suite**

`pytest -q`
Expected: green.

- [ ] **Step 2: Lint**

`ruff check core/ blueprints/ tests/`
`mypy --ignore-missing-imports core/ blueprints/`
Expected: clean.

- [ ] **Step 3: Self-review checklist**

Walk every checkbox; confirm:
- All 2FA secrets are Fernet-encrypted in DB (no plaintext `totp_secret_encrypted`).
- All login/recovery codes are bcrypt-hashed (never sha256 — recovery codes need cost).
- The pending-totp secret in the Flask session is the encrypted form, never plaintext.
- Login-from-new-device fires only on first sighting (insert), not updates.
- Org `require_2fa` enforcement exempts `/settings/2fa`, `/logout`, `/static`, `/login`.
- Recovery flow is the only path that clears TOTP without verifying current TOTP.
- Audit `metadata` is JSON-serialized; PII is bounded (note truncated, error truncated).
- Archive job is idempotent (`ON CONFLICT(id)` not needed because rows are deleted from source; assert the test confirms re-running with no old rows returns 0).
- APScheduler is NOT started under `TESTING=True` to avoid runaway timers in CI.

- [ ] **Step 4: PR description draft (`docs/pr-gamma.md`)**

```markdown
# PR-γ — 2FA + Recovery + Audit Log

Implements Phase γ of the multi-tenant rollout. Adds TOTP and email-based
two-factor authentication, single-use bcrypt-hashed backup recovery codes, an
admin-approved out-of-band recovery flow, login-from-new-device email
notifications, org-level "Require 2FA" enforcement, an org-scoped audit log of
every privileged action, a program-owner cross-org audit search, and a nightly
APScheduler job that archives audit entries older than 365 days.

## What's new

- `core/totp.py`, `core/qrcode_render.py` — RFC 6238 + QR PNG render
- `core/recovery.py` — bcrypt-hashed single-use backup codes
- `core/email_2fa.py` — 6-digit emailed login code (hashed, 10-min TTL)
- `core/audit.py`, `core/audit_archive.py` — writer + nightly rotation
- `core/login_notifications.py` — first-sighting (user, ip) emailer
- `core/recovery_request.py` — admin-approved recovery flow
- `blueprints/twofa.py`, `blueprints/recovery.py`, `blueprints/audit.py`
- Extended `blueprints/auth.py` with `/login/2fa`, `/login/email-2fa`
- `before_request` 2FA enforcement when org `require_2fa=1`
- Audit hooks across every privileged action

## Schema additions

- `email_2fa_codes` table (hash, expires_at, used_at)
- `login_ip_sightings` table (user_id, ip, first_seen, last_seen)
- `users.notify_new_device` BOOLEAN

(`recovery_codes`, `recovery_requests`, `audit_log`, `audit_log_archive` were
forward-declared in PR-α.)

## Migration risk

- New columns / tables only — no destructive changes.
- APScheduler boot is gated by `TESTING` to keep CI clean.
- 2FA is opt-in per user; enforcement is opt-in per org.

## Test coverage

- TOTP, QR, recovery, email-2fa, audit, audit-archive, recovery flow,
  require-2fa enforcement, login-notifications, scheduler-boot.
- New integration: full login → 2FA → upload audit trail.

## Operator notes

- Set `BASE_URL=https://autoalert.pro` (used in recovery + new-device emails).
- Confirm Resend templates pass DKIM after this deploy.
- The first APScheduler run is at 03:00 UTC the day after deploy.
```

- [ ] **Step 5: Final commit + open PR (left for the orchestrator)**

The orchestrator opens the PR with the description above; no further commit here.

---
