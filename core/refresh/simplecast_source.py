"""Scrape SimpleCast episode list using the saved Playwright session."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from core.calendar_refresh import ExternalItem, SessionExpiredError
from core.playwright_session import (
    PlaywrightSession,
    SessionConfig,
    url_marker_login_check,
)

_log = logging.getLogger(__name__)

NAME = "simplecast"
PLATFORMS = ["simplecast"]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SESSION_FILE = _PROJECT_ROOT / "simplecast_session.json"

_DEFAULT_LIST_URL = (
    "https://dashboard.simplecast.com/accounts/"
    "fbbdf431-bf12-4f72-acd7-0fb0bdd6c798/shows/"
    "e64f6974-8126-44da-92e3-4ead66d25d01/episodes"
)
_LOGIN_MARKERS = ("/login", "/signin", "/sign-in", "auth0", "/oauth")
_DISPLAY_TZ = ZoneInfo("America/New_York")
_ROW_DT_RE = re.compile(
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s+\d{4}\s+at\s+\d{1,2}:\d{2}\s+(?:AM|PM)",
    re.I,
)


def _classify_status(badge: str) -> str | None:
    """Map a SimpleCast episode-list badge to a calendar status.

    Published episodes render with **no** badge in the dashboard table;
    Scheduled (future) episodes carry a "Scheduled" badge. Anything else
    (Draft / Private) isn't a calendar item, so it's skipped.
    """
    b = (badge or "").strip().lower()
    if b == "scheduled":
        return "scheduled"
    if b in ("", "published"):
        return "published"
    return None


def _rows_to_items(rows, window_start: date, window_end: date) -> list[ExternalItem]:
    """Pure transform: scraped DOM rows -> windowed ExternalItems.

    Kept Playwright-free so the status/date/title parsing is unit-testable
    against captured row shapes.
    """
    out: list[ExternalItem] = []
    for r in rows:
        status = _classify_status(r.get("badge", ""))
        if status is None:
            continue
        dt = _parse_row_datetime(r.get("rowText") or "")
        if not dt:
            continue
        d = dt.date()
        if not (window_start <= d <= window_end):
            continue
        lines = [ln.strip() for ln in (r.get("rowText") or "").splitlines() if ln.strip()]
        lines = [ln for ln in lines if "scheduled" not in ln.lower()
                 and "published" not in ln.lower()
                 and not _ROW_DT_RE.search(ln)]
        title = max(lines, key=len) if lines else ""
        out.append(ExternalItem(
            platform="simplecast",
            external_id=r["id"],
            iso_date=d.isoformat(),
            scheduled_time=dt.isoformat(),
            title=title,
            url=r["href"],
            status=status,
            raw_json=json.dumps({"badge": r.get("badge", "")}),
        ))
    return out


def _list_url() -> str:
    explicit = os.environ.get("SIMPLECAST_UPLOAD_URL", "").strip()
    if explicit:
        # The uploader URL points at /episodes/new — strip /new.
        return explicit.rstrip("/").removesuffix("/new")
    return _DEFAULT_LIST_URL


def _parse_row_datetime(text: str) -> datetime | None:
    m = _ROW_DT_RE.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), "%B %d, %Y at %I:%M %p").replace(tzinfo=_DISPLAY_TZ)
    except ValueError:
        try:
            return datetime.strptime(m.group(0), "%b %d, %Y at %I:%M %p").replace(tzinfo=_DISPLAY_TZ)
        except ValueError:
            return None


_SESSION_CFG = SessionConfig(
    name="simplecast",
    session_file=str(_SESSION_FILE),
    is_login_url=url_marker_login_check(_LOGIN_MARKERS),
    target_url="",  # set per-call from env/config/default
    headless_env="SIMPLECAST_HEADLESS",
    chrome_path_env="SIMPLECAST_CHROME_PATH",
    no_login_recovery=True,
    default_timeout_ms=60_000,
)


def fetch(window_start: date, window_end: date) -> list[ExternalItem]:
    if not _SESSION_FILE.exists():
        raise SessionExpiredError("simplecast_session.json missing")

    cfg = SessionConfig(
        name=_SESSION_CFG.name,
        session_file=_SESSION_CFG.session_file,
        is_login_url=_SESSION_CFG.is_login_url,
        target_url=_list_url(),
        headless_env=_SESSION_CFG.headless_env,
        chrome_path_env=_SESSION_CFG.chrome_path_env,
        no_login_recovery=True,
        default_timeout_ms=_SESSION_CFG.default_timeout_ms,
    )

    out: list[ExternalItem] = []
    with PlaywrightSession(cfg) as sess:
        page = sess.page
        assert page is not None

        # PlaywrightSession navigates with wait_until=domcontentloaded; the
        # episodes list lazy-loads rows after that, so wait for the network
        # to settle before scanning the DOM.
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception as e:  # noqa: BLE001 — networkidle may never fire; scan anyway
            _log.debug("simplecast: networkidle wait timed out, proceeding: %s", e)

        try:
            page.wait_for_selector("a[href*='/episodes/']", timeout=15_000)
        except Exception:
            return []

        # Try clicking "Load more" up to 5 times
        for _ in range(5):
            btn = page.query_selector("button:has-text('Load more'), button:has-text('Show more')")
            if not btn:
                break
            try:
                btn.click(timeout=2000)
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                break

        rows = page.evaluate(r"""
            () => {
              const seen = new Map();
              document.querySelectorAll('a[href*="/episodes/"]').forEach(a => {
                const m = a.href.match(/\/episodes\/([0-9a-f-]{36})/i);
                if (!m) return;
                const id = m[1];
                const tr = a.closest('tr');
                if (!tr) return;
                if (!seen.has(id)) {
                  const badge = tr.querySelector('.badge__content');
                  seen.set(id, {
                    id,
                    href: a.href,
                    rowText: tr.innerText,
                    badge: badge ? badge.innerText.trim() : '',
                  });
                }
              });
              return Array.from(seen.values());
            }
        """)
        # storage_state is auto-saved by PlaywrightSession on exit.
        out = _rows_to_items(rows, window_start, window_end)
    return out
