"""Scrape Rock content channel listings via Playwright using the saved session."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path

import yaml

from core.calendar_refresh import ExternalItem, SessionExpiredError
from core.playwright_session import (
    PlaywrightSession,
    SessionConfig,
    has_session,
)

log = logging.getLogger(__name__)

NAME = "rock"
PLATFORMS = ["rock"]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SESSION_FILE = _PROJECT_ROOT / "rock_session.json"
_CONFIG_FILE = _PROJECT_ROOT / "config.yaml"
_BASE_URL = os.environ.get("ROCK_BASE_URL", "https://rock.lcbcchurch.com").rstrip("/")

# Rock's login page is page id 3. Match `/page/3` only as the *whole* segment
# (followed by end, slash, or query) so we don't false-positive on legitimate
# pages like /page/343 (the content-channel listing).
_LOGIN_PAGE_RE = re.compile(r"/page/3(?:[/?]|$)")

# Refreshes are unattended — default to headless so we don't pop a Chrome
# window during a calendar refresh. ROCK_REFRESH_HEADED=true forces headed
# (e.g. for debugging selectors).
_HEADED = os.environ.get("ROCK_REFRESH_HEADED", "").lower() in ("1", "true", "yes")


def _looks_like_login(url: str) -> bool:
    return "/Login" in url or bool(_LOGIN_PAGE_RE.search(url))


def _channel_guids() -> list[str]:
    """Read configured guids from config.yaml; return [] if absent."""
    if not _CONFIG_FILE.exists():
        return []
    with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return list((cfg.get("calendar_refresh") or {}).get("rock_channel_guids") or [])


def _channel_list_url(guid: str) -> str:
    return f"{_BASE_URL}/page/343?ContentChannelGuid={guid}"


def _parse_date(text: str) -> date | None:
    text = (text or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _expand_page_size(page) -> None:
    """Click the largest page-size pager link if available so we capture the
    full configured window. Rock's listing defaults to 50 items per page;
    pager options usually include 50/500/5000. We pick the largest <=5000."""
    try:
        sizes = page.evaluate("""() => {
            const links = Array.from(
                document.querySelectorAll('.grid-pagesize a, .grid-pager a')
            );
            return links
                .map(a => ({text: a.innerText.trim(), id: a.id}))
                .filter(o => /^\\d+$/.test(o.text));
        }""")
        if not sizes:
            return
        # Pick the largest size that's still sane (cap at 5000 so we don't
        # ask Rock for "all" and time out the page).
        sizes.sort(key=lambda s: int(s["text"]), reverse=True)
        chosen = next((s for s in sizes if int(s["text"]) <= 5000), sizes[-1])
        if chosen["text"] == "50":
            return  # already on default
        page.locator(f"#{chosen['id']}").click()
        page.wait_for_load_state("networkidle", timeout=30_000)
        # Re-wait for table to repopulate
        page.wait_for_selector(".grid-table tbody tr", timeout=20_000)
    except Exception as exc:
        log.debug("Could not expand Rock page size: %s", exc)


def _build_session_cfg(target_url: str) -> SessionConfig:
    """Build a SessionConfig for the refresh source.

    Used by both rock_source.fetch and rock_email_source.fetch — they share
    the same session file (the user logs into Rock once). ``target_url`` is
    the first channel listing PlaywrightSession should navigate to; per-guid
    loops still call ``page.goto`` directly.
    """
    return SessionConfig(
        name="rock",
        session_file=str(_SESSION_FILE),
        is_login_url=_looks_like_login,
        target_url=target_url,
        chrome_path_env="ROCK_CHROME_PATH",
        default_headless=not _HEADED,
        no_login_recovery=True,
        default_timeout_ms=60_000,
    )


def _scrape_channel(page, guid: str, window_start: date,
                    window_end: date,
                    today: date | None = None) -> list[ExternalItem]:
    """Load one channel's listing and emit ExternalItems.

    Split out of ``fetch`` so ``rock_email_source`` can reuse the navigation
    + login-detection + page-size-expansion plumbing without depending on
    PlaywrightSession lifecycle details.

    *today* defaults to the system date. Per-row status is "published" when
    the row's date is on/before today, "scheduled" when in the future —
    matching `rock_email_source._rows_to_items`. Without this gate every
    row collapsed to a hardcoded "active" string, which fell through the
    calendar's status-rank table to the default-"published" branch and
    surfaced future dates as already-sent.
    """
    if today is None:
        today = datetime.now().date()
    url = _channel_list_url(guid)
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    if _looks_like_login(page.url):
        raise SessionExpiredError("redirected to login")
    try:
        page.wait_for_selector(".grid-table tbody tr", timeout=20_000)
    except Exception:
        log.warning("Rock refresh: no rows for channel %s", guid)
        return []
    _expand_page_size(page)
    page.wait_for_timeout(500)

    # Rock uses a non-standard `datakey` attribute (NOT `data-datakey`),
    # so tr.dataset.datakey is undefined. Use getAttribute. The Title
    # cell is index 0 and the Date cell is index 2 — verified via
    # Playwright probe 2026-04-28 against /page/343.
    rows = page.evaluate("""
        () => {
          return Array.from(document.querySelectorAll('.grid-table tbody tr'))
            .map(tr => ({
                id: tr.getAttribute('datakey') || '',
                cells: Array.from(tr.cells).map(c => c.innerText.trim()),
            }))
            .filter(r => r.id);
        }
    """)
    log.info("Rock refresh: channel %s yielded %d rows", guid, len(rows))

    out: list[ExternalItem] = []
    for r in rows:
        cells = r["cells"]
        title = cells[0] if cells else ""
        date_text = cells[2] if len(cells) > 2 else ""
        d = _parse_date(date_text)
        if not d:
            continue
        if not (window_start <= d <= window_end):
            continue
        out.append(ExternalItem(
            platform="rock",
            external_id=str(r["id"]),
            iso_date=d.isoformat(),
            scheduled_time="",
            title=title,
            url=f"{_BASE_URL}/ContentChannelItem/{r['id']}",
            status="published" if d <= today else "scheduled",
            raw_json=json.dumps({"channel_guid": guid}),
        ))
    return out


def fetch(window_start: date, window_end: date) -> list[ExternalItem]:
    # Guard on the encrypted store, not the on-disk file: PlaywrightSession
    # removes the materialized file on exit and re-creates it from the store
    # on enter, so a prior run leaves no file even though the session is fine.
    # Post-migrate_secrets the plaintext rock_session.json is shredded — only
    # the encrypted blob in the store exists. has_session() checks both.
    # Pass org_id so the check reads the active tenant's slot, not the
    # (empty post-wipe) legacy unscoped slot. Inside a refresh worker
    # thread effective_org_id() reads the thread-local override set by
    # core.calendar_refresh._fetch_one.
    from core.org_context import effective_org_id
    if not has_session(str(_SESSION_FILE), org_id=effective_org_id()):
        raise SessionExpiredError("rock_session.json missing")

    guids = _channel_guids()
    if not guids:
        return []

    cfg = _build_session_cfg(_channel_list_url(guids[0]))

    out: list[ExternalItem] = []
    with PlaywrightSession(cfg) as sess:
        page = sess.page
        assert page is not None
        # PlaywrightSession already navigated to the first guid; bail
        # early if that hit a login redirect.
        if _looks_like_login(page.url):
            raise SessionExpiredError("redirected to login")
        for guid in guids:
            out.extend(_scrape_channel(page, guid, window_start, window_end))
        # storage_state is re-saved + persisted to the encrypted store by
        # PlaywrightSession.__exit__; no manual persist needed here.
    return out
