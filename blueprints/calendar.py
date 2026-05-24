"""Month-grid calendar view + refresh endpoint."""
from __future__ import annotations

import calendar as _cal
from datetime import date as _date, datetime

from flask import Blueprint, current_app, jsonify, render_template, request

from core import db as _db
from core.calendar_refresh_view import merge_for_window
from core.config import load_config

bp = Blueprint("calendar", __name__)


# Canonicalize raw platform labels into the chip ids the template uses
# (yt_video / yt_shorts / podcast / rock / rock_email / instagram / facebook).
# upload_history stores display strings ("YouTube Video"); external_calendar_items
# stores slugs ("youtube_video"). Without this, filter chips show 0,
# pills get no color, and dedup misses across the two tables.
_CHIP_KEY = {
    "youtube video": "yt_video",
    "youtube_video": "yt_video",
    "youtube shorts": "yt_shorts",
    "youtube_shorts": "yt_shorts",
    "simplecast": "podcast",
    "podcast": "podcast",
    "rock": "rock",
    "rock_email": "rock_email",
    "rock email": "rock_email",
    "instagram": "instagram",
    "facebook": "facebook",
    # Vista Social uploads are scheduled to IG+FB. The upload_history row
    # uses the display string "Vista Social"; map it to a chip so the
    # calendar still renders a pill (we bucket it under instagram by
    # default since the IG character limit is the binding constraint).
    "vista social": "instagram",
    "vista_social": "instagram",
}


def _chip_key(p: str) -> str:
    return _CHIP_KEY.get((p or "").strip().lower(), (p or "").strip().lower())


# The UI exposes exactly these three status filter chips. Any other value
# coming back from a refresh source (e.g. Rock emits "active") would render
# a pill, but the JS chip filter would immediately hide it because its
# data-status isn't in this set. Normalize at the route boundary so the
# template and JS see only known values.
_KNOWN_STATUSES = {"published", "scheduled", "failed"}


def _normalize_status(s: str) -> str:
    s = (s or "").strip().lower()
    if s in _KNOWN_STATUSES:
        return s
    # An item returned by an external source is, by definition, present on
    # the platform — treat unknown statuses as "published" so the user
    # actually sees them. (Rock's "active" is the canonical example.)
    return "published"


def _parse_time(sched: str) -> str:
    """Best-effort HH:MM extraction from a scheduled_time string.

    Tolerates: ISO-8601 with or without timezone, "YYYY-MM-DD HH:MM",
    and bare "HH:MM". Returns "" if nothing parses.
    """
    if not sched:
        return ""
    s = sched.strip()
    try:
        return datetime.fromisoformat(s).strftime("%H:%M")
    except ValueError:
        pass
    # "YYYY-MM-DD HH:MM" (space, no T)
    if len(s) >= 16 and s[10] == " ":
        try:
            return datetime.strptime(s[:16], "%Y-%m-%d %H:%M").strftime("%H:%M")
        except ValueError:
            pass
    # Bare "HH:MM"
    if len(s) >= 5 and s[2] == ":":
        try:
            return datetime.strptime(s[:5], "%H:%M").strftime("%H:%M")
        except ValueError:
            pass
    return ""


def _coerce_date(s: str):
    if not s:
        return None
    # iso_date is typically "YYYY-MM-DD"; tolerate other formats too
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


@bp.route("/calendar")
def calendar_view():
    """Month-grid view of scheduled & published uploads, sourced from upload_history."""
    ym = request.args.get("ym", "")
    today = _date.today()
    try:
        year, month = (int(x) for x in ym.split("-", 1))
        if not (1 <= month <= 12):
            raise ValueError
    except Exception:
        year, month = today.year, today.month

    # Build the month grid first so we know exactly which dates are visible
    # (the Sun-anchored view shows up to ~6 days from each adjacent month).
    cal = _cal.Calendar(firstweekday=6)  # 6 = Sunday
    weeks = cal.monthdatescalendar(year, month)
    visible_dates = {d for week in weeks for d in week}
    iso_start = min(visible_dates).isoformat()
    iso_end = max(visible_dates).isoformat()

    # Date-windowed query rather than LIMIT-based; without this, months whose
    # rows have been pushed past the limit by newer uploads silently render
    # empty even though the count badges appear correct elsewhere.
    # Scoped to the active org (impersonation-aware): when acting as a tenant,
    # the calendar shows that tenant's runs only.
    from core.org_context import effective_org_id
    history_records = _db.get_history_for_window(
        iso_start, iso_end, org_id=effective_org_id(),
    )

    history_for_merge: list[dict] = []
    for r in history_records:
        d = _coerce_date(r.get("iso_date") or "")
        if d is None or d not in visible_dates:
            continue
        if r.get("success"):
            status = "published"
        elif r.get("error"):
            status = "failed"
        else:
            status = "scheduled"
        sched = (r.get("scheduled_time") or "").strip()
        time_str = _parse_time(sched)
        raw_platform = r.get("platform") or ""
        history_for_merge.append({
            "id": r.get("id"),
            # Keep the raw platform string — merge_for_window's provider bucket
            # canonicalizes both display and slug forms for dedup.
            "platform": raw_platform,
            "_chip": _chip_key(raw_platform),
            "external_id": r.get("external_id") or "",
            "title": r.get("title") or "(untitled)",
            "url": r.get("url") or "",
            "scheduled_time": sched,
            "iso_date": d.isoformat(),
            "_day": d.day,
            "_time": time_str,
            "_status": status,
            "error": r.get("error") or "",
            "file_path": r.get("file_path") or "",
        })

    external_records = _db.get_external_items_for_window(
        iso_start, iso_end, org_id=effective_org_id(),
    )
    external_for_merge: list[dict] = []
    for r in external_records:
        d = _coerce_date(r.get("iso_date") or "")
        if d is None or d not in visible_dates:
            continue
        sched = (r.get("scheduled_time") or "").strip()
        time_str = _parse_time(sched)
        raw_platform = r.get("platform") or ""
        external_for_merge.append({
            "id": r.get("id"),
            "platform": raw_platform,
            "_chip": _chip_key(raw_platform),
            "external_id": r.get("external_id") or "",
            "title": r.get("title") or "(untitled)",
            "url": r.get("url") or "",
            "scheduled_time": sched,
            "iso_date": d.isoformat(),
            "_day": d.day,
            "_time": time_str,
            "_status": _normalize_status(r.get("status") or ""),
        })

    merged = merge_for_window(history_for_merge, external_for_merge)

    posts: list[dict] = []
    for m in merged:
        posts.append({
            "id": m.get("id"),
            "day": m.get("_day"),
            # Hand the chip id (yt_video/yt_shorts/podcast/...) to the template;
            # the raw platform label is only useful for dedup which has already run.
            "platform": m.get("_chip") or "",
            "title": m.get("title") or "(untitled)",
            "time": m.get("_time") or "",
            "status": _normalize_status(m.get("_status") or ""),
            "url": m.get("url") or "",
            "error": m.get("error", "") or "",
            "iso_date": m.get("iso_date") or "",
            "scheduled_time": m.get("scheduled_time") or "",
            "file_path": m.get("file_path", "") or "",
            "source": m.get("source", "upload"),
        })

    # Bucket posts by full ISO date so adjacent-month grid cells get their
    # events too (the previous version filtered to current-month-only, which
    # made the trailing/leading days of the visible grid look empty).
    posts_by_iso: dict[str, list[dict]] = {}
    for p in posts:
        posts_by_iso.setdefault(p["iso_date"], []).append(p)

    grid = []
    for week in weeks:
        row = []
        for d in week:
            cell_posts = list(posts_by_iso.get(d.isoformat(), []))
            cell_posts.sort(key=lambda p: (p["time"] or "99:99", p["platform"]))
            row.append({
                "day": d.day,
                "iso": d.isoformat(),
                "in_month": d.month == month,
                "is_today": d == today,
                "posts": cell_posts,
            })
        grid.append(row)

    platform_counts: dict[str, int] = {}
    status_counts = {"published": 0, "scheduled": 0, "failed": 0}
    for p in posts:
        platform_counts[p["platform"]] = platform_counts.get(p["platform"], 0) + 1
        status_counts[p["status"]] = status_counts.get(p["status"], 0) + 1

    prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_month = (year + 1, 1) if month == 12 else (year, month + 1)

    month_label = _date(year, month, 1).strftime("%B %Y")
    weekday_labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    return render_template(
        "calendar.html",
        year=year,
        month=month,
        month_label=month_label,
        prev_ym=f"{prev_month[0]}-{prev_month[1]:02d}",
        next_ym=f"{next_month[0]}-{next_month[1]:02d}",
        today_ym=f"{today.year}-{today.month:02d}",
        grid=grid,
        weekday_labels=weekday_labels,
        platform_counts=platform_counts,
        status_counts=status_counts,
        total_visible=len(posts),
    )


@bp.route("/calendar/refresh", methods=["POST"])
def calendar_refresh_endpoint():
    """Run all configured calendar-refresh sources in parallel."""
    from core import calendar_refresh as _cr
    sources = _cr.get_configured_sources()

    cfg = (load_config().get("calendar_refresh") or {})

    def _cfg_int(key: str, default: int) -> int:
        # L17: a malformed config value (e.g. "30 days") used to 500 the
        # /calendar/refresh route. Fall back to the default with a warning.
        try:
            return int(cfg.get(key, default))
        except (TypeError, ValueError):
            current_app.logger.warning("calendar_refresh.%s is not an int — using default %d", key, default)
            return default

    result = _cr.run_refresh(
        sources=sources,
        window_days_back=_cfg_int("window_days_back", 30),
        window_days_forward=_cfg_int("window_days_forward", 180),
        source_timeout_sec=_cfg_int("source_timeout_sec", 180),
    )
    if result.get("busy"):
        return jsonify({"error": "refresh already in progress"}), 409
    return jsonify(result)
