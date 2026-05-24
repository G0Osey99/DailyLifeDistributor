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
    legacy = _migrate_legacy_secret_names(org["id"])
    log.info(
        "Migration: backfilled %d device rows, %d secret rows, %d history rows; "
        "moved %d legacy secrets to org scope and %d to platform scope.",
        d, s, h, legacy["moved_to_org"], legacy["moved_to_platform"],
    )
    if legacy["moved_to_org"] or legacy["moved_to_platform"]:
        from core import audit
        audit.write_event(
            action="system.legacy_secret_migration",
            actor_user_id=None,
            org_id=org["id"],
            metadata=legacy,
        )
