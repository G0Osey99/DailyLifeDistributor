"""Shared test helpers for Phase γ multi-tenant 2FA / audit tests.

These were referenced by the plan as if PR-β established them; in practice
the existing PR-α/β suites use inline DB seeding. We centralize the helpers
here so the γ tests stay readable and mirror the plan's vocabulary.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core import db as _db
from core import user_store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_user(
    db_module=None,
    *,
    username: str,
    email: str | None = None,
    password: str = "long-enough-pw-12!",
    totp_enabled: bool = False,
    totp_secret_encrypted: str | None = None,
    email_2fa_enabled: bool = False,
    program_owner: bool = False,
    notify_new_device: bool = True,
) -> dict:
    """Create a user row and return it as a dict.

    Mirrors the plan's signature: `make_user(db, username=..., ...)`. The
    `db_module` positional is accepted for compatibility but ignored — we
    always go through the shared db module.
    """
    email = email or f"{username}@example.com"
    user = user_store.create_user(
        username=username,
        email=email,
        password=password,
        program_owner=program_owner,
    )
    # New users have password_changed_at=NULL; flip to now so verify_password works.
    user_store.update_password(user["id"], password)
    # Optionally seed 2FA flags + the optional encrypted TOTP secret.
    fields = []
    params: list = []
    if totp_enabled:
        fields.append("totp_enabled=?")
        params.append(1)
    if totp_secret_encrypted is not None:
        fields.append("totp_secret_encrypted=?")
        params.append(totp_secret_encrypted)
    if email_2fa_enabled:
        fields.append("email_2fa_enabled=?")
        params.append(1)
    # notify_new_device defaults to 1 in the schema (added by Task 27);
    # only push an UPDATE when the caller explicitly wants 0.
    if not notify_new_device:
        fields.append("notify_new_device=?")
        params.append(0)
    if fields:
        params.append(user["id"])
        with _db._get_conn() as c:
            c.execute(
                f"UPDATE users SET {', '.join(fields)} WHERE id=?",
                tuple(params),
            )
            c.commit()
    # Re-fetch so the returned dict reflects any updates.
    fresh = user_store.get_user_by_id(user["id"])
    return fresh


def make_org(db_module=None, name: str = "Test Org", *, require_2fa: bool = False, slug: str | None = None) -> dict:
    """Insert an organization row and return it as a dict."""
    slug = slug or name.lower().replace(" ", "-")
    with _db._get_conn() as c:
        cur = c.execute(
            "INSERT INTO organizations "
            "(name, slug, plan, require_2fa, created_at) "
            "VALUES (?, ?, 'free', ?, ?)",
            (name, slug, 1 if require_2fa else 0, _now()),
        )
        c.commit()
        oid = cur.lastrowid
        row = c.execute("SELECT * FROM organizations WHERE id=?", (oid,)).fetchone()
    return dict(row)


def add_membership(db_module, user_id: int, org_id: int, *, role: str = "user") -> None:
    """Insert an org_memberships row."""
    with _db._get_conn() as c:
        c.execute(
            "INSERT INTO org_memberships (user_id, org_id, role, joined_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, org_id, role, _now()),
        )
        c.commit()


def login_as(client, user: dict, current_org_id: int | None = None) -> None:
    """Prime the test client's session as the given user.

    Skips the password flow; tests for the login form itself use real POSTs.
    """
    with client.session_transaction() as s:
        s["user_id"] = user["id"]
        if current_org_id is not None:
            s["current_org_id"] = current_org_id


def last_email(captured_emails, template: str | None = None) -> dict:
    """Return the most recent captured email (optionally filtered by template).

    Helper supports either positional usage `last_email(captured, "x")` or
    keyword usage. The plan's snippets sometimes call it both ways.
    """
    if template is None:
        return captured_emails[-1]
    matches = [m for m in captured_emails if m["template"] == template]
    if not matches:
        raise AssertionError(f"No email captured with template={template!r}")
    return matches[-1]
