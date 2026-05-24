"""Scrape the Rock "Daily Life" *email* content channel for the calendar.

This is the calendar-refresh counterpart to the Rock Email uploader
(`uploaders/rock/email.py`). It surfaces the scheduled/sent email items so
the calendar can differentiate the Daily Experience (in-app) Rock posts
(`rock` chip) from the email broadcasts (`rock_email` chip).

Unlike the Daily Experience listing (Title@0, Date@2), the email channel's
grid has a different column layout — verified live 2026-05-22:

    ['Title', 'Thumbnail', 'YouTube Link', 'Sent',
     'Youtube Daily Life Media Sync', 'Start', '', 'Expire', ...]

so the date ("Start") sits at index 5, not 2. We therefore locate the date
and title columns by **header name** rather than a fixed index, which keeps
this robust if Rock's channel columns are reordered.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from core.calendar_refresh import ExternalItem, SessionExpiredError
from core.playwright_session import PlaywrightSession, has_session
from core.refresh.rock_source import (
    _BASE_URL,
    _SESSION_FILE,
    _build_session_cfg,
    _channel_list_url,
    _expand_page_size,
    _looks_like_login,
    _parse_date,
)

log = logging.getLogger(__name__)

NAME = "rock_email"
PLATFORMS = ["rock_email"]

# Default to the live email channel guid; overridable via config so the grab
# isn't hard-coded to one show.
try:
    from uploaders.rock.constants import _CHANNEL_GUID_EMAIL as _DEFAULT_EMAIL_GUID
except Exception:  # pragma: no cover - constants always present in practice
    _DEFAULT_EMAIL_GUID = "2182c1f3-8f8c-44f3-987f-75a698fe44a7"

# Header labels we accept for the date / title columns, in priority order.
_DATE_HEADERS = ("start", "date")
_TITLE_HEADERS = ("title",)


def _email_channel_guids() -> list[str]:
    """Read configured email-channel guids from config.yaml; fall back to the
    known production channel so the grab works out of the box."""
    from core.refresh.rock_source import _CONFIG_FILE
    if _CONFIG_FILE.exists():
        import yaml
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        guids = (cfg.get("calendar_refresh") or {}).get("rock_email_channel_guids")
        if guids:
            return list(guids)
    return [_DEFAULT_EMAIL_GUID]


def _col_index(headers: list[str], wanted: tuple[str, ...]) -> int | None:
    lowered = [(h or "").strip().lower() for h in headers]
    for w in wanted:
        if w in lowered:
            return lowered.index(w)
    return None


def _rows_to_items(headers, rows, window_start: date, window_end: date,
                   today: date, guid: str = "") -> list[ExternalItem]:
    """Pure transform: scraped grid (headers + rows) -> windowed ExternalItems.

    Kept Playwright-free so the header-based column detection and the
    date/status logic are unit-testable against captured grid shapes.
    """
    date_idx = _col_index(headers, _DATE_HEADERS)
    title_idx = _col_index(headers, _TITLE_HEADERS)
    if title_idx is None:
        title_idx = 0
    if date_idx is None:
        log.warning("Rock email refresh: no Start/Date column in headers %s", headers)
        return []

    out: list[ExternalItem] = []
    for r in rows:
        cells = r.get("cells") or []
        if len(cells) <= date_idx:
            continue
        d = _parse_date(cells[date_idx])
        if not d:
            continue
        if not (window_start <= d <= window_end):
            continue
        title = cells[title_idx] if len(cells) > title_idx else ""
        out.append(ExternalItem(
            platform="rock_email",
            external_id=str(r.get("id", "")),
            iso_date=d.isoformat(),
            scheduled_time="",
            title=title,
            url=f"{_BASE_URL}/ContentChannelItem/{r.get('id', '')}",
            status="published" if d <= today else "scheduled",
            raw_json=json.dumps({"channel_guid": guid}),
        ))
    return out


def _scrape_email_channel(page, guid: str, window_start: date,
                          window_end: date, today: date) -> list[ExternalItem]:
    """Drive one email channel's listing and emit ExternalItems."""
    page.goto(_channel_list_url(guid), wait_until="domcontentloaded", timeout=60_000)
    if _looks_like_login(page.url):
        raise SessionExpiredError("redirected to login")
    try:
        page.wait_for_selector(".grid-table tbody tr", timeout=20_000)
    except Exception:
        log.warning("Rock email refresh: no rows for channel %s", guid)
        return []
    _expand_page_size(page)
    page.wait_for_timeout(500)

    data = page.evaluate("""
        () => {
          const heads = Array.from(
            document.querySelectorAll('.grid-table thead th')
          ).map(t => t.innerText.trim());
          const rows = Array.from(document.querySelectorAll('.grid-table tbody tr'))
            .map(tr => ({
                id: tr.getAttribute('datakey') || '',
                cells: Array.from(tr.cells).map(c => c.innerText.trim()),
            }))
            .filter(r => r.id);
          return {heads, rows};
        }
    """)
    headers = data.get("heads") or []
    rows = data.get("rows") or []
    log.info("Rock email refresh: channel %s yielded %d rows", guid, len(rows))
    return _rows_to_items(
        headers, rows, window_start, window_end, today, guid=guid)


def fetch(window_start: date, window_end: date) -> list[ExternalItem]:
    # Guard on the encrypted store, not the on-disk file — see rock_source.fetch
    # for the full reasoning. Post-migrate_secrets the plaintext file is
    # shredded; has_session() also accepts an in-store blob. org_id picks
    # the active tenant's slot (refresh worker's thread-local override).
    from core.org_context import effective_org_id
    if not has_session(str(_SESSION_FILE), org_id=effective_org_id()):
        raise SessionExpiredError("rock_session.json missing")

    guids = _email_channel_guids()
    if not guids:
        return []

    today = date.today()
    cfg = _build_session_cfg(_channel_list_url(guids[0]))
    out: list[ExternalItem] = []
    with PlaywrightSession(cfg) as sess:
        page = sess.page
        assert page is not None
        if _looks_like_login(page.url):
            raise SessionExpiredError("redirected to login")
        for guid in guids:
            out.extend(_scrape_email_channel(
                page, guid, window_start, window_end, today))
        # PlaywrightSession.__exit__ atomically saves + persists to store.
    return out
