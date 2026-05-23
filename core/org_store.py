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
