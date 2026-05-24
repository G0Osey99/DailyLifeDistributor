"""YouTube Data API quota tracking.

Quota is stored in SQLite (table ``youtube_quota``) keyed by date in
``America/Los_Angeles``, which is when Google's daily quota actually
resets. This makes the counter persist across Flask sessions, browser
restarts, and concurrent refreshes — anything that calls the YouTube API
in this process contributes to the same daily counter.

Cost table is sourced from the YouTube Data API v3 quota docs:
  - videos.insert         = 1600
  - thumbnails.set        = 50
  - channels.list         = 1
  - playlistItems.list    = 1
  - videos.list           = 1
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

# Shares the same SQLite file as core.db; honor the same DLD_STATE_DB override
# so the hosted deploy's mounted volume covers quota state too.
_DB_PATH = os.environ.get("DLD_STATE_DB") or os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "state.db"
)
_RESET_TZ = ZoneInfo("America/Los_Angeles")

QUOTA_COSTS = {
    # Upload-side actions (charged on success in the SSE stream).
    "video_upload": 1600,
    "shorts_upload": 1600,
    "thumbnail_set": 50,
    "shorts_thumbnail": 50,
    # Refresh-side actions (charged per API call inside youtube_source).
    "refresh_channels_list": 1,
    "refresh_playlist_items_list": 1,
    "refresh_videos_list": 1,
    "refresh_search_list": 100,  # search.list is 100 units/call (YouTube quota docs)
}
def _load_daily_quota() -> int:
    """Read youtube.daily_quota from config.yaml; fall back to Google's default."""
    try:
        from core.config import load_config
        cfg = load_config() or {}
        value = (cfg.get("youtube") or {}).get("daily_quota", 10000)
        return int(value)
    except Exception:
        # Bad YAML / missing section / non-numeric value — silently
        # using 10000 hides config drift. Log once at import time.
        import logging
        logging.getLogger(__name__).warning(
            "core.quota: could not read youtube.daily_quota from config; "
            "falling back to 10000", exc_info=True,
        )
        return 10000


DAILY_QUOTA = _load_daily_quota()


def _today_key() -> str:
    return datetime.now(_RESET_TZ).date().isoformat()


@contextmanager
def _conn():
    """Yield a SQLite connection and reliably close it on exit.

    `with sqlite3.connect(...)` only commits/rolls back, it does not close,
    so the previous form leaked handles in long-lived processes.
    """
    c = sqlite3.connect(_DB_PATH, timeout=10.0, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS youtube_quota ("
        "  quota_date TEXT PRIMARY KEY,"
        "  units_used INTEGER NOT NULL DEFAULT 0"
        ")"
    )


def track_quota_usage(action: str, units: int | None = None) -> None:
    """Add the cost of ``action`` to today's quota counter.

    ``units`` overrides the table lookup when provided (used by callers
    that already know the exact cost, e.g. variable-cost API calls).
    Unknown actions with no explicit ``units`` are a no-op so callers can
    add new actions without crashing older code paths.
    """
    cost = units if units is not None else QUOTA_COSTS.get(action, 0)
    if cost <= 0:
        return
    key = _today_key()
    with _conn() as c:
        _ensure_table(c)
        c.execute(
            "INSERT INTO youtube_quota (quota_date, units_used) VALUES (?, ?) "
            "ON CONFLICT(quota_date) DO UPDATE SET units_used = units_used + excluded.units_used",
            (key, cost),
        )
        c.commit()


def get_quota_used() -> int:
    """Return units used today (Pacific time). 0 if no row exists yet."""
    key = _today_key()
    with _conn() as c:
        _ensure_table(c)
        row = c.execute(
            "SELECT units_used FROM youtube_quota WHERE quota_date = ?", (key,)
        ).fetchone()
        return int(row["units_used"]) if row else 0


# ---------- Multi-tenant phase δ: per-org quota tracking ----------

def track_org_quota_usage(
    org_id: int, action: str, units: int | None = None
) -> None:
    """Per-org counterpart of ``track_quota_usage``.

    Backed by the ``yt_quota_usage`` table (created in
    ``core.db.init_db()`` with primary key (org_id, quota_date)). Same
    QUOTA_COSTS lookup; unknown actions with no explicit ``units`` are a
    no-op so callers can add new actions without crashing older paths.
    """
    cost = units if units is not None else QUOTA_COSTS.get(action, 0)
    if cost <= 0:
        return
    key = _today_key()
    with _conn() as c:
        c.execute(
            "INSERT INTO yt_quota_usage (org_id, quota_date, units_used) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(org_id, quota_date) "
            "DO UPDATE SET units_used = units_used + excluded.units_used",
            (org_id, key, cost),
        )
        c.commit()


def get_org_quota_used(org_id: int) -> int:
    """Return units used today (Pacific time) for ``org_id``. 0 if none."""
    key = _today_key()
    with _conn() as c:
        row = c.execute(
            "SELECT units_used FROM yt_quota_usage "
            "WHERE org_id = ? AND quota_date = ?",
            (org_id, key),
        ).fetchone()
        return int(row["units_used"]) if row else 0
