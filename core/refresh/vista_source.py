r"""Scrape Vista Social calendar to surface scheduled IG + FB posts.

Vista's `/calendar` page renders a FullCalendar grid where every event
is a small link. Critically, Vista renders **one event per (post,
network)** pair — a post that goes to Facebook and Instagram appears
twice on the same day, each with its own network icon. We turn each
event into one ExternalItem keyed on the (Vista post id, network) pair.

Markup we depend on (verified via the live dashboard):
  * Day cells: `[data-date="YYYY-MM-DD"]` (FullCalendar standard).
  * Event link: `a.fc-daygrid-event` containing an inner div with a
    `name="<post-id>"` attribute.
  * Two `<img>` children per event: one is the profile thumbnail
    (URL contains `/networks/<id>/profile.jpeg`), the other is a
    network-overlay svg whose URL ends in `/<network>.svg`.
  * Month-nav arrows: `button[class*="StyledDateNavigationButton"]`,
    rendered left-then-right (prev, next).
  * Month header: an element matching `/^[A-Za-z]+\s+\d{4}$/` near the
    top of the page (e.g. "April 2026").
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

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

NAME = "vista_social"
PLATFORMS = ["instagram", "facebook"]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SESSION_FILE = _PROJECT_ROOT / "vista_social_session.json"

_CALENDAR_URL = "https://vistasocial.com/calendar"
_LOGIN_MARKERS = ("/login", "/signin", "/sign-in", "/oauth", "auth0")
_DISPLAY_TZ = ZoneInfo("America/New_York")

_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*([ap])", re.I)


# ---------------------------------------------------------------------------
# Page-level helpers (executed in the browser context)
# ---------------------------------------------------------------------------

# Per-event extraction. Returns one entry per visible event with:
#   isoDate, postId, network, text, isStory
#
# Story detection: Vista renders a small post-type icon inside each event;
# stories use a distinctive circle-arc icon whose SVG path starts with the
# signature below. Regular posts (image, video, reel, document, etc.) use
# other paths (a calendar-grid icon, a play-arrow, etc.). The signature is
# stable across the calendar's pagination — confirmed against multiple
# stories and posts in the live dashboard.
_STORY_ICON_SIGNATURE = "M9.19 12.003h2.784M11.974"

_PAGE_DUMP_JS = r"""
(storySig) => {
  const events = Array.from(document.querySelectorAll('a.fc-daygrid-event'));
  const out = [];
  for (const ev of events) {
    const dayCell = ev.closest('[data-date]');
    const isoDate = dayCell ? dayCell.getAttribute('data-date') : '';
    if (!isoDate || !/^\d{4}-\d{2}-\d{2}$/.test(isoDate)) continue;

    const text = (ev.innerText || ev.textContent || '').trim();

    // Inner div with name="<post-id>" — Vista's id for the post.
    const idHolder = ev.querySelector('[name]');
    const postId = idHolder ? idHolder.getAttribute('name') : '';

    // Network overlay svg img — pick the one whose URL ends with
    // /<network>.svg, ignoring the profile thumbnail.
    let network = '';
    for (const img of ev.querySelectorAll('img')) {
      const m = (img.src || '').match(/\/([a-z_]+)\.svg(?:\?|$)/i);
      if (m) {
        const n = m[1].toLowerCase();
        if (n !== 'profile' && n !== 'networks') {
          network = n;
          break;
        }
      }
    }

    // Detect Instagram Stories by their post-type icon's SVG path.
    let isStory = false;
    for (const svg of ev.querySelectorAll('svg path')) {
      const d = svg.getAttribute('d') || '';
      if (d.startsWith(storySig)) { isStory = true; break; }
    }

    out.push({ isoDate, postId, network, text, isStory });
  }
  return out;
}
"""

_HEADER_JS = r"""
() => {
  const candidates = Array.from(document.querySelectorAll('*'))
    .filter(el => {
      if (!el.offsetParent) return false;
      const r = el.getBoundingClientRect();
      if (r.y > 100 || r.x < 300 || r.x > 1200) return false;
      const t = (el.innerText || '').trim();
      return /^[A-Za-z]+\s+\d{4}$/.test(t);
    });
  return candidates.length ? candidates[0].innerText.trim() : '';
}
"""

# Click the prev or next month arrow. The two `StyledDateNavigationButton`
# instances are rendered left-to-right (prev, next); we sort by x to be
# robust against layout changes that might reorder them in the DOM.
_NAV_CLICK_JS = r"""
(forward) => {
  const btns = Array.from(
    document.querySelectorAll('button[class*="StyledDateNavigationButton"]')
  ).filter(b => b.offsetParent !== null);
  if (btns.length < 2) return false;
  btns.sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x);
  const target = forward ? btns[btns.length - 1] : btns[0];
  target.click();
  return true;
}
"""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_event_time(text: str, iso_date: str) -> datetime | None:
    """Parse a leading time prefix like ``7:01a`` or ``3a`` into a datetime
    in the display timezone. Returns None if no time prefix is found."""
    if not text:
        return None
    head = text.strip().split("\n", 1)[0].strip()
    m = _TIME_RE.match(head)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3).lower()
    if ampm == "p" and hour != 12:
        hour += 12
    elif ampm == "a" and hour == 12:
        hour = 0
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=_DISPLAY_TZ)


def _strip_time_prefix(text: str) -> str:
    """Drop the leading time token (e.g. ``7:01a``) from event text so the
    remainder reads as a real title."""
    head, _, rest = text.partition("\n")
    head = _TIME_RE.sub("", head, count=1).strip()
    return ("\n".join([head, rest]) if rest else head).strip() or text.strip()


# ---------------------------------------------------------------------------
# Month-by-month traversal
# ---------------------------------------------------------------------------

def _month_step(d: date, n: int) -> date:
    m = d.month + n
    y = d.year + (m - 1) // 12
    m = ((m - 1) % 12) + 1
    return date(y, m, 1)


def _read_current_month(page) -> date | None:
    text = page.evaluate(_HEADER_JS)
    try:
        dt = datetime.strptime(text, "%B %Y")
    except ValueError:
        return None
    return date(dt.year, dt.month, 1)


def _navigate_to_month(page, target: date, max_clicks: int = 240) -> bool:
    """Click the prev/next arrow until the visible month equals `target`.

    After each click we poll the header until the text actually changes
    (or 5 s elapses) before reading it again — without this, FullCalendar
    re-renders slower than our wait and we over-click into the wrong
    month.
    """
    for _ in range(max_clicks):
        current = _read_current_month(page)
        if current == target:
            return True
        if current is None:
            page.wait_for_timeout(300)
            continue

        forward = target > current
        before_label = current.strftime("%Y-%m")
        ok = page.evaluate(_NAV_CLICK_JS, forward)
        if not ok:
            return False

        # Wait for the header to change off `before_label` (or up to 5 s).
        change_deadline = 5_000
        elapsed = 0
        while elapsed < change_deadline:
            page.wait_for_timeout(150)
            elapsed += 150
            after = _read_current_month(page)
            if after is not None and after.strftime("%Y-%m") != before_label:
                break
    return False


def _capture_events_in_window(page, start: date, end: date) -> list[dict]:
    """Walk the calendar one month at a time across [start, end] and
    accumulate raw events deduped by (post_id, network, iso_date)."""
    aggregate: dict[tuple, dict] = {}

    # Always include events from the initially-rendered month too.
    for ev in (page.evaluate(_PAGE_DUMP_JS, _STORY_ICON_SIGNATURE) or []):
        key = (ev.get("postId") or "", ev.get("network") or "", ev.get("isoDate") or "")
        aggregate[key] = ev

    # Build month list inclusive of start_month..end_month.
    start_month = date(start.year, start.month, 1)
    end_month = date(end.year, end.month, 1)
    target = start_month
    while target <= end_month:
        ok = _navigate_to_month(page, target)
        if not ok:
            logger.warning("vista_source: failed to navigate to %s", target)
            break

        # The header label flips before FullCalendar finishes mounting
        # the new month's day grid. Wait until a day cell whose
        # data-date is in the target month appears, capped at 5 s.
        prefix = target.strftime("%Y-%m")
        try:
            page.wait_for_function(
                """(prefix) => {
                    const cells = document.querySelectorAll('[data-date]');
                    for (const c of cells) {
                        const d = c.getAttribute('data-date') || '';
                        if (d.startsWith(prefix)) return true;
                    }
                    return false;
                }""",
                arg=prefix,
                timeout=5_000,
            )
        except Exception:
            logger.warning("vista_source: day grid for %s never rendered", target)

        for ev in (page.evaluate(_PAGE_DUMP_JS, _STORY_ICON_SIGNATURE) or []):
            key = (ev.get("postId") or "",
                   ev.get("network") or "",
                   ev.get("isoDate") or "")
            aggregate[key] = ev

        target = _month_step(target, 1)

    return list(aggregate.values())


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_SESSION_CFG = SessionConfig(
    name="vista_social",
    session_file=str(_SESSION_FILE),
    is_login_url=url_marker_login_check(_LOGIN_MARKERS),
    target_url=_CALENDAR_URL,
    headless_env="VISTA_SOCIAL_HEADLESS",
    # Without this the refresh launch falls back to channel='chrome' and fails
    # on the arm64 VPS (no Google Chrome); the env var points at chromium.
    chrome_path_env="VISTA_SOCIAL_CHROME_PATH",
    # Refresh is a non-interactive scrape — default to headless. The uploader
    # has its own first-run-headed flow; refresh never needs that. Set
    # VISTA_SOCIAL_HEADLESS=false to debug selectors.
    default_headless=True,
    no_login_recovery=True,
    viewport={"width": 1440, "height": 900},
    default_timeout_ms=60_000,
)


def fetch(window_start: date, window_end: date) -> list[ExternalItem]:
    if not _SESSION_FILE.exists():
        raise SessionExpiredError("vista_social_session.json missing")

    out: list[ExternalItem] = []
    today = date.today()

    with PlaywrightSession(_SESSION_CFG) as sess:
        page = sess.page
        assert page is not None

        # PlaywrightSession navigates with wait_until=domcontentloaded; let
        # FullCalendar finish its async hydration before we read the DOM.
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception as e:  # noqa: BLE001 — networkidle may never fire; scan anyway
            logger.debug("vista: networkidle wait timed out, proceeding: %s", e)

        # Wait for FullCalendar to render at least one day cell.
        try:
            page.wait_for_selector("[data-date]", timeout=20_000)
        except Exception:
            return []

        raw_events = _capture_events_in_window(page, window_start, window_end)

        # storage_state is auto-saved by PlaywrightSession on exit.
        for ev in raw_events:
            iso = ev.get("isoDate") or ""
            if not iso:
                continue
            try:
                d = datetime.strptime(iso, "%Y-%m-%d").date()
            except ValueError:
                continue
            if not (window_start <= d <= window_end):
                continue

            network = (ev.get("network") or "").lower()
            if network not in PLATFORMS:
                # YouTube and any other future network — skip; the
                # YouTube source is the source of truth for that
                # platform, and unknown networks would clutter the
                # calendar without a chip mapping.
                continue

            # Drop Instagram Stories. The uploader doesn't schedule
            # stories (only feed posts to IG + FB) and stories are
            # managed manually outside this app, so surfacing them
            # would just create dedup noise on the calendar.
            if ev.get("isStory"):
                continue

            text = ev.get("text") or ""
            dt = _parse_event_time(text, iso)
            title = _strip_time_prefix(text) or "(untitled)"

            post_id = ev.get("postId") or ""
            # Per-(post, network) external_id so dedup works across
            # repeated refreshes. Fall back to a date+title hash if
            # Vista didn't expose a post id.
            external_id = (
                f"{post_id}-{network}" if post_id
                else f"{iso}-{network}-{title[:60]}"
            )

            out.append(ExternalItem(
                platform=network,
                external_id=external_id,
                iso_date=d.isoformat(),
                scheduled_time=dt.isoformat() if dt else "",
                title=title.strip(),
                url="",  # Vista doesn't expose a per-post deep link in the calendar event
                status="published" if d <= today else "scheduled",
                raw_json=json.dumps({
                    "post_id": post_id,
                    "network": network,
                    "raw_text": text,
                }),
            ))
    return out
