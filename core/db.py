"""Thin SQLite wrapper for session persistence and upload history."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

# DLD_STATE_DB lets a hosted deploy point the SQLite file at a mounted volume
# so the encrypted secret store / sessions survive container restarts. Unset
# (local/USB) keeps the repo-root state.db.
_DB_PATH = os.environ.get("DLD_STATE_DB") or os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "state.db"
)


@contextmanager
def _get_conn():
    # check_same_thread=False is safe here because every helper opens a
    # short-lived connection within a single function call. WAL mode lets
    # the upload worker pool record_upload() concurrently without "database
    # is locked" errors under default rollback journaling.
    #
    # Wrapped as a context manager so the connection is reliably closed on
    # exit. The previous `with sqlite3.connect(...)` form only commits or
    # rolls back; long-lived processes accumulated handles otherwise.
    conn = sqlite3.connect(_DB_PATH, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # H10: belt-and-suspenders busy timeout. The connect()-level timeout
    # only applies to the initial open; this PRAGMA makes SQLite retry
    # internally on a locked db (USB drive contention with 4 parallel
    # uploaders + reaper + SSE consumer) for up to 30 seconds.
    try:
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    try:
        yield conn
    finally:
        conn.close()


# Set once per process; init_db() flips this on after PRAGMAs are applied so
# we don't re-issue them on every connection.
_PRAGMAS_APPLIED = False


def init_db() -> None:
    """Create tables if they do not exist. Call once at app startup."""
    global _PRAGMAS_APPLIED
    with _get_conn() as conn:
        if not _PRAGMAS_APPLIED:
            # state.db lives on the USB drive that travels with the app.
            # SQLite's official advice is to NOT use WAL on removable / network
            # storage: a yank between WAL write and checkpoint can corrupt the
            # main DB file (this is the symptom app.py's startup recovery has
            # been working around). TRUNCATE journaling + busy_timeout=30s gives
            # us safe single-writer semantics with our low write volume
            # (4 uploaders + reaper + SSE — all millisecond-scale writes).
            # synchronous=FULL is the default; we keep it for crash safety.
            try:
                conn.execute("PRAGMA journal_mode=TRUNCATE")
            except sqlite3.Error:
                pass
            _PRAGMAS_APPLIED = True
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at TEXT,
                updated_at TEXT,
                label TEXT,
                state_json TEXT,
                status TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS upload_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                uploaded_at TEXT,
                iso_date TEXT,
                platform TEXT,
                title TEXT,
                file_path TEXT,
                success INTEGER,
                url TEXT,
                scheduled_time TEXT,
                error TEXT
            )
        """)
        # Image history for the Rock Vista background-image gatherer.
        # photo_id is the stock-API id (Unsplash/Pexels). source flags which.
        # used_on_date is the publish date the image was used for, so we
        # can dedupe by topic recency and photo recency separately.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS image_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id TEXT,
                source TEXT,
                topic TEXT,
                used_on_date TEXT,
                photographer TEXT,
                photo_url TEXT,
                recorded_at TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_image_history_photo "
            "ON image_history(source, photo_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_image_history_topic "
            "ON image_history(topic, used_on_date)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS external_calendar_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                iso_date TEXT NOT NULL,
                scheduled_time TEXT,
                title TEXT,
                url TEXT,
                status TEXT,
                raw_json TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
                UNIQUE(platform, external_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ext_iso_date "
            "ON external_calendar_items(iso_date)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_devices (
                id TEXT PRIMARY KEY,
                name TEXT,
                token_hash TEXT NOT NULL,
                created_at TEXT,
                last_seen_at TEXT,
                revoked INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_pairing_codes (
                code_hash TEXT PRIMARY KEY,
                created_at TEXT,
                expires_at TEXT,
                consumed INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS secrets (
                name TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                value BLOB NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
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
        # Phase γ: 2FA + audit log additions.
        # users.totp_enabled — boolean flag separate from totp_secret_encrypted
        # so we can disable without dropping the secret (and vice versa).
        ucols = {r[1] for r in conn.execute(
            "PRAGMA table_info('users')").fetchall()}
        if "totp_enabled" not in ucols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0"
            )
        if "notify_new_device" not in ucols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN notify_new_device INTEGER NOT NULL DEFAULT 1"
            )
        # recovery_requests.note — free-text "what happened" context from the user.
        rrcols = {r[1] for r in conn.execute(
            "PRAGMA table_info('recovery_requests')").fetchall()}
        if "note" not in rrcols:
            conn.execute("ALTER TABLE recovery_requests ADD COLUMN note TEXT")
        # Email 2FA single-use codes (10-min TTL, bcrypt-hashed).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_2fa_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                code_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_2fa_codes_user "
            "ON email_2fa_codes(user_id, used_at)"
        )
        # Login-from-new-device sighting log: emits an email on first
        # (user_id, ip) pair so a stolen credential triggers a heads-up.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_ip_sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ip TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                UNIQUE(user_id, ip)
            )
        """)
        # Idempotent ALTER for the new external_id column on upload_history
        cols = [r[1] for r in conn.execute("PRAGMA table_info('upload_history')").fetchall()]
        if "external_id" not in cols:
            conn.execute("ALTER TABLE upload_history ADD COLUMN external_id TEXT")
        # Idempotent ALTERs for HWID + hostname columns on agent_devices
        # (Phase 3.5: HWID-tagged device records). Both nullable; older
        # rows from before this migration sit as NULL and the list/online
        # endpoints handle that.
        dcols = {r[1] for r in conn.execute(
            "PRAGMA table_info('agent_devices')").fetchall()}
        if "hwid_hash" not in dcols:
            conn.execute("ALTER TABLE agent_devices ADD COLUMN hwid_hash TEXT")
        if "hostname" not in dcols:
            conn.execute("ALTER TABLE agent_devices ADD COLUMN hostname TEXT")
        # Multi-tenant phase α: agent_devices.user_id (nullable).
        if "user_id" not in dcols:
            conn.execute("ALTER TABLE agent_devices ADD COLUMN user_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_devices_user "
            "ON agent_devices(user_id)"
        )
        # Multi-tenant phase α: secrets.org_id (nullable).
        scols = {r[1] for r in conn.execute(
            "PRAGMA table_info('secrets')").fetchall()}
        if "org_id" not in scols:
            conn.execute("ALTER TABLE secrets ADD COLUMN org_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_secrets_org ON secrets(org_id)"
        )
        # Multi-tenant phase α: upload_history.org_id + user_id (nullable).
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
        # Multi-tenant phase δ: per-org per-platform soft mutex used by the
        # web upload dispatch. Primary key (org_id, platform) gives us a
        # single holder per pair; expires_at lets a stale lock auto-release
        # 30 minutes after acquisition.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS platform_locks (
                org_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                locked_by_user_id INTEGER NOT NULL,
                locked_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (org_id, platform)
            )
        """)
        # Multi-tenant phase δ: per-org daily YouTube API quota usage.
        # Same QUOTA_COSTS table as the legacy single-tenant counter; this
        # one is scoped per-org so one tenant's heavy refresh day doesn't
        # blow another's daily 10K cap.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS yt_quota_usage (
                org_id INTEGER NOT NULL,
                quota_date TEXT NOT NULL,
                units_used INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (org_id, quota_date)
            )
        """)
        # Per-org configuration overlay. Each (org_id, section) holds a JSON
        # blob that overrides the corresponding subtree in config.yaml when
        # read via core.config.effective_config(org_id). Today's known
        # sections: "scheduling", "description_footers". Adding more sections
        # is a write-shape choice in core.org_settings, not a schema change.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS org_settings (
                org_id INTEGER NOT NULL,
                section TEXT NOT NULL,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (org_id, section)
            )
        """)
        conn.commit()


def save_session(session_id: str, label: str, state_json: str, status: str) -> None:
    """Upsert a session row atomically."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sessions (id, created_at, updated_at, label, state_json, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                updated_at = excluded.updated_at,
                label      = excluded.label,
                state_json = excluded.state_json,
                status     = excluded.status
            """,
            (session_id, now, now, label, state_json, status),
        )
        conn.commit()


def load_session(session_id: str) -> dict | None:
    """Fetch one session row as a dict, or None if not found."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None


def get_latest_in_progress() -> dict | None:
    """Return the most recent in_progress session, or None."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE status = 'in_progress' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def list_sessions(limit: int = 50) -> list[dict]:
    """Return up to *limit* sessions ordered by most recent first."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def complete_session(session_id: str) -> None:
    """Mark a session as completed."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET status='completed', updated_at=? WHERE id=?",
            (now, session_id),
        )
        conn.commit()


def record_upload(
    session_id: str,
    iso_date: str,
    platform: str,
    title: str,
    file_path: str,
    success: bool,
    url: str,
    scheduled_time: str,
    error: str,
    external_id: str | None = None,
) -> None:
    """Insert a row into upload_history. If `external_id` is None, parse it from `url`."""
    from core.refresh.id_extract import parse_url
    if external_id is None:
        external_id = parse_url(platform, url) or ""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO upload_history "
            "(session_id, uploaded_at, iso_date, platform, title, file_path, "
            " success, url, scheduled_time, error, external_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                now,
                iso_date,
                platform,
                title,
                file_path or "",
                1 if success else 0,
                url or "",
                scheduled_time or "",
                error or "",
                external_id or "",
            ),
        )
        conn.commit()


def has_successful_upload(session_id: str, iso_date: str, platform: str) -> bool:
    """True if a prior *successful* upload exists for this (session, date, platform).

    Powers the idempotent re-run guard: a batch re-run skips any (date,
    platform) already recorded as a success, so a re-run after a mid-run tab
    close never double-uploads a YouTube video / SimpleCast draft.
    """
    if not session_id:
        return False
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM upload_history "
            "WHERE session_id=? AND iso_date=? AND platform=? AND success=1 LIMIT 1",
            (session_id, iso_date, platform),
        ).fetchone()
        return row is not None


def backfill_external_ids() -> int:
    """Populate upload_history.external_id for legacy rows that lack it.

    Idempotent: rows with a non-empty external_id are left alone.
    Returns the number of rows updated. Cheap when there's nothing to do —
    the WHERE clause filters at the SQL level so a no-op startup costs one
    indexed query rather than a full-table scan in Python.
    """
    import logging
    from core.refresh.id_extract import parse_url
    log = logging.getLogger(__name__)
    updated = 0
    with _get_conn() as conn:
        # Short-circuit: if there are no rows missing an external_id, skip
        # the SELECT + per-row update entirely.
        (pending,) = conn.execute(
            "SELECT COUNT(*) FROM upload_history "
            "WHERE (external_id IS NULL OR external_id='') AND COALESCE(url,'') != ''"
        ).fetchone()
        if not pending:
            return 0

        rows = conn.execute(
            "SELECT id, platform, url FROM upload_history "
            "WHERE (external_id IS NULL OR external_id='') AND COALESCE(url,'') != ''"
        ).fetchall()
        # Per-row try/except so one bad URL never aborts startup. The whole
        # block runs in a single transaction; we just skip and continue.
        for r in rows:
            try:
                ext = parse_url(r["platform"] or "", r["url"] or "")
                if ext:
                    conn.execute(
                        "UPDATE upload_history SET external_id=? WHERE id=?",
                        (ext, r["id"]),
                    )
                    updated += 1
            except Exception as e:
                log.warning("backfill_external_ids: skipping row %s: %s", r["id"], e)
        conn.commit()
    return updated


# ---------- External calendar items ----------

def upsert_external_items(items: list[dict]) -> None:
    """Insert-or-update rows in external_calendar_items keyed by (platform, external_id)."""
    if not items:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        for it in items:
            conn.execute(
                """
                INSERT INTO external_calendar_items
                    (platform, external_id, iso_date, scheduled_time, title, url,
                     status, raw_json, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, external_id) DO UPDATE SET
                    iso_date       = excluded.iso_date,
                    scheduled_time = excluded.scheduled_time,
                    title          = excluded.title,
                    url            = excluded.url,
                    status         = excluded.status,
                    raw_json       = excluded.raw_json,
                    last_seen_at   = excluded.last_seen_at
                """,
                (
                    it["platform"], it["external_id"], it["iso_date"],
                    it.get("scheduled_time", ""), it.get("title", ""), it.get("url", ""),
                    it.get("status", ""), it.get("raw_json", ""),
                    now, now,
                ),
            )
        conn.commit()


def mark_stale_external_items(
    platform: str, iso_start: str, iso_end: str, seen_ids: set[str]
) -> int:
    """Flip status='deleted' for rows in [iso_start, iso_end] for this platform
    whose external_id is NOT in `seen_ids`. Returns affected row count.
    """
    with _get_conn() as conn:
        if seen_ids:
            qmarks = ",".join("?" for _ in seen_ids)
            cur = conn.execute(
                f"""UPDATE external_calendar_items
                    SET status='deleted'
                    WHERE platform = ?
                      AND iso_date BETWEEN ? AND ?
                      AND external_id NOT IN ({qmarks})
                      AND status != 'deleted'""",
                (platform, iso_start, iso_end, *seen_ids),
            )
        else:
            cur = conn.execute(
                """UPDATE external_calendar_items
                    SET status='deleted'
                    WHERE platform = ?
                      AND iso_date BETWEEN ? AND ?
                      AND status != 'deleted'""",
                (platform, iso_start, iso_end),
            )
        conn.commit()
        return cur.rowcount


def get_external_items_for_window(iso_start: str, iso_end: str) -> list[dict]:
    """Return non-deleted external items with iso_date in [iso_start, iso_end]."""
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM external_calendar_items
               WHERE iso_date BETWEEN ? AND ?
                 AND COALESCE(status,'') != 'deleted'
               ORDER BY iso_date, scheduled_time, platform""",
            (iso_start, iso_end),
        ).fetchall()
        return [dict(r) for r in rows]


def recent_photo_ids(source: str, since_iso_date: str) -> set[str]:
    """Return photo ids from `source` used on/after `since_iso_date`."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT photo_id FROM image_history "
            "WHERE source=? AND used_on_date >= ?",
            (source, since_iso_date),
        ).fetchall()
        return {r["photo_id"] for r in rows}


def recent_topics(since_iso_date: str) -> set[str]:
    """Return topics used on/after `since_iso_date`, lower-cased."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT topic FROM image_history WHERE used_on_date >= ?",
            (since_iso_date,),
        ).fetchall()
        return {(r["topic"] or "").lower() for r in rows}


def record_image_use(
    photo_id: str,
    source: str,
    topic: str,
    used_on_date: str,
    photographer: str = "",
    photo_url: str = "",
) -> None:
    """Insert a row into image_history once a photo has been successfully used."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO image_history "
            "(photo_id, source, topic, used_on_date, photographer, photo_url, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (photo_id, source, topic, used_on_date, photographer, photo_url, now),
        )
        conn.commit()


def get_history(session_id: str | None = None, limit: int = 100) -> list[dict]:
    """Fetch upload_history rows, optionally filtered by session_id."""
    with _get_conn() as conn:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM upload_history WHERE session_id=? ORDER BY uploaded_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM upload_history ORDER BY uploaded_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


# ---------- Phase γ: 2FA + audit log helpers ----------
# Thin pass-throughs so blueprints can call `core.db.get_user_by_id(...)`
# without importing `core.user_store` — the plan and the new modules
# both speak directly to `core.db`.


def get_user_by_id(user_id: int) -> dict | None:
    from core import user_store
    return user_store.get_user_by_id(user_id)


def get_user_by_username(username: str) -> dict | None:
    from core import user_store
    return user_store.get_user_by_username(username)


def get_membership(user_id: int, org_id: int) -> dict | None:
    from core import org_store
    return org_store.get_membership(user_id=user_id, org_id=org_id)


def get_org(org_id: int) -> dict | None:
    with _get_conn() as c:
        row = c.execute(
            "SELECT * FROM organizations WHERE id=?", (org_id,)
        ).fetchone()
    return dict(row) if row else None


def set_user_totp(user_id: int, encrypted_secret: str | None, enabled: bool) -> None:
    with _get_conn() as c:
        c.execute(
            "UPDATE users SET totp_secret_encrypted=?, totp_enabled=? WHERE id=?",
            (encrypted_secret, 1 if enabled else 0, user_id),
        )
        c.commit()


def set_user_email_2fa(user_id: int, enabled: bool) -> None:
    with _get_conn() as c:
        c.execute(
            "UPDATE users SET email_2fa_enabled=? WHERE id=?",
            (1 if enabled else 0, user_id),
        )
        c.commit()


def insert_recovery_code(*, user_id: int, code_hash: str, created_at: str) -> int:
    with _get_conn() as c:
        cur = c.execute(
            "INSERT INTO recovery_codes (user_id, code_hash, created_at) "
            "VALUES (?, ?, ?)",
            (user_id, code_hash, created_at),
        )
        c.commit()
        return cur.lastrowid


def list_recovery_codes(user_id: int) -> list[dict]:
    with _get_conn() as c:
        rows = c.execute(
            "SELECT id, code_hash, used_at, created_at "
            "FROM recovery_codes WHERE user_id=? ORDER BY id",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_recovery_code_used(code_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as c:
        c.execute(
            "UPDATE recovery_codes SET used_at=? WHERE id=? AND used_at IS NULL",
            (now, code_id),
        )
        c.commit()


def delete_recovery_codes(user_id: int) -> None:
    with _get_conn() as c:
        c.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))
        c.commit()


def insert_email_2fa_code(*, user_id: int, code_hash: str, expires_at: str, created_at: str) -> int:
    with _get_conn() as c:
        cur = c.execute(
            "INSERT INTO email_2fa_codes "
            "(user_id, code_hash, expires_at, created_at) VALUES (?,?,?,?)",
            (user_id, code_hash, expires_at, created_at),
        )
        c.commit()
        return cur.lastrowid


def get_unused_email_2fa_codes(user_id: int) -> list[dict]:
    with _get_conn() as c:
        rows = c.execute(
            "SELECT id, code_hash, expires_at FROM email_2fa_codes "
            "WHERE user_id=? AND used_at IS NULL ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_email_2fa_code_used(code_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as c:
        c.execute(
            "UPDATE email_2fa_codes SET used_at=? WHERE id=?",
            (now, code_id),
        )
        c.commit()


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


def list_audit_events(
    *,
    org_id: int | None = None,
    limit: int = 100,
    actor_user_id: int | None = None,
    action_prefix: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
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
    with _get_conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def archive_audit_batch(cutoff_iso: str, batch_size: int) -> int:
    with _get_conn() as c:
        rows = c.execute(
            "SELECT id FROM audit_log WHERE created_at<? ORDER BY id LIMIT ?",
            (cutoff_iso, batch_size),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        c.execute(
            f"INSERT INTO audit_log_archive "
            f"  (id, org_id, actor_user_id, action, target_type, target_id, "
            f"   metadata, ip, user_agent, created_at) "
            f"SELECT id, org_id, actor_user_id, action, target_type, target_id, "
            f"       metadata, ip, user_agent, created_at "
            f"FROM audit_log WHERE id IN ({placeholders})",
            ids,
        )
        c.execute(f"DELETE FROM audit_log WHERE id IN ({placeholders})", ids)
        c.commit()
        return len(ids)


def list_audit_archive(limit: int = 100) -> list[dict]:
    with _get_conn() as c:
        rows = c.execute(
            "SELECT * FROM audit_log_archive ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def insert_recovery_request(*, user_id: int, requested_at: str, expires_at: str, note: str) -> int:
    with _get_conn() as c:
        cur = c.execute(
            "INSERT INTO recovery_requests "
            "(user_id, requested_at, expires_at, note) VALUES (?,?,?,?)",
            (user_id, requested_at, expires_at, note),
        )
        c.commit()
        return cur.lastrowid


def count_recovery_requests_since(user_id: int, since_iso: str) -> int:
    with _get_conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS c FROM recovery_requests "
            "WHERE user_id=? AND requested_at>=?",
            (user_id, since_iso),
        ).fetchone()
    return int(row["c"])


def list_org_owners_for_user(user_id: int) -> list[dict]:
    with _get_conn() as c:
        rows = c.execute(
            "SELECT DISTINCT u.* FROM users u "
            "JOIN org_memberships om2 ON om2.user_id = u.id AND om2.role='owner' "
            "WHERE om2.org_id IN ("
            "  SELECT org_id FROM org_memberships WHERE user_id=?"
            ")",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recovery_request(rid: int) -> dict | None:
    with _get_conn() as c:
        row = c.execute(
            "SELECT * FROM recovery_requests WHERE id=?", (rid,)
        ).fetchone()
    return dict(row) if row else None


def list_recovery_requests() -> list[dict]:
    with _get_conn() as c:
        rows = c.execute(
            "SELECT * FROM recovery_requests ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_recovery_request_approve(rid: int, approver_user_id: int, approved_at: str, token: str) -> None:
    import hashlib
    h = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _get_conn() as c:
        c.execute(
            "UPDATE recovery_requests "
            "SET approver_user_id=?, approved_at=?, password_reset_token_hash=? "
            "WHERE id=?",
            (approver_user_id, approved_at, h, rid),
        )
        c.commit()


def consume_recovery_request(rid: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as c:
        c.execute(
            "UPDATE recovery_requests SET consumed_at=? WHERE id=?",
            (now, rid),
        )
        c.commit()


def user_owns_any_org_with(approver_id: int, target_user_id: int) -> bool:
    with _get_conn() as c:
        row = c.execute(
            "SELECT 1 FROM org_memberships a "
            "JOIN org_memberships b ON a.org_id = b.org_id "
            "WHERE a.user_id=? AND a.role='owner' AND b.user_id=? LIMIT 1",
            (approver_id, target_user_id),
        ).fetchone()
    return row is not None


def get_login_ip_sighting(user_id: int, ip: str) -> dict | None:
    with _get_conn() as c:
        row = c.execute(
            "SELECT * FROM login_ip_sightings WHERE user_id=? AND ip=?",
            (user_id, ip),
        ).fetchone()
    return dict(row) if row else None


def upsert_login_ip_sighting(user_id: int, ip: str, now_iso: str) -> bool:
    """Insert-or-update; returns True iff this is a brand-new (user, ip) pair."""
    with _get_conn() as c:
        existing = c.execute(
            "SELECT 1 FROM login_ip_sightings WHERE user_id=? AND ip=?",
            (user_id, ip),
        ).fetchone()
        if existing is None:
            c.execute(
                "INSERT INTO login_ip_sightings "
                "(user_id, ip, first_seen, last_seen) VALUES (?,?,?,?)",
                (user_id, ip, now_iso, now_iso),
            )
            c.commit()
            return True
        c.execute(
            "UPDATE login_ip_sightings SET last_seen=? WHERE user_id=? AND ip=?",
            (now_iso, user_id, ip),
        )
        c.commit()
        return False


def set_user_notify_new_device(user_id: int, enabled: bool) -> None:
    with _get_conn() as c:
        c.execute(
            "UPDATE users SET notify_new_device=? WHERE id=?",
            (1 if enabled else 0, user_id),
        )
        c.commit()


def set_org_require_2fa(org_id: int, enabled: bool) -> None:
    with _get_conn() as c:
        c.execute(
            "UPDATE organizations SET require_2fa=? WHERE id=?",
            (1 if enabled else 0, org_id),
        )
        c.commit()


def get_history_for_window(iso_start: str, iso_end: str) -> list[dict]:
    """Fetch upload_history rows whose iso_date falls in [iso_start, iso_end].

    Used by the calendar view, which needs every record in the visible month
    regardless of how deep it sits in history. The previous LIMIT-based query
    silently dropped older months once the table grew past the cap.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM upload_history
               WHERE iso_date BETWEEN ? AND ?
               ORDER BY iso_date, scheduled_time, platform""",
            (iso_start, iso_end),
        ).fetchall()
        return [dict(r) for r in rows]
