"""Thin SQLite wrapper for session persistence and upload history."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "state.db")


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
            CREATE TABLE IF NOT EXISTS secrets (
                name TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                value BLOB NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Idempotent ALTER for the new external_id column on upload_history
        cols = [r[1] for r in conn.execute("PRAGMA table_info('upload_history')").fetchall()]
        if "external_id" not in cols:
            conn.execute("ALTER TABLE upload_history ADD COLUMN external_id TEXT")
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
