"""Vista Social uploader — Playwright-driven dashboard automation.

Same session pattern as the Simplecast uploader:

* FIRST RUN — a Chrome window opens to vistasocial.com/login and waits
  for the user to log in manually. On success, cookies + local storage
  are saved to `vista_social_session.json` at the project root (on the
  USB), so the login carries from machine to machine.

* EVERY RUN AFTER THAT — the saved session is loaded and the post is
  scheduled without a login step.

Vista Social has no usable public API for our scheduling needs, so we
drive the dashboard the way a human would: open the calendar page, click
Create → New post, deselect the YouTube profile, fill caption, attach
the shorts video, choose a Schedule datetime, and submit.

REQUIREMENTS:
    pip install playwright

    No `playwright install` step is needed — this module drives the
    Google Chrome that's already on the machine via Playwright's
    `channel='chrome'`, matching `simplecast_uploader.py`.

Environment variables (all optional):
    VISTA_SOCIAL_HEADLESS       "true" to hide the automation window
                                once a session is cached. First-time
                                login is always headed. Default: false
    VISTA_SOCIAL_LOGIN_TIMEOUT  Seconds to wait for the user to log in
                                on first run. Default: 300 (5 minutes)
    VISTA_SOCIAL_CHROME_PATH    Full path to Chrome if non-standard
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
except ImportError:
    PlaywrightTimeout = Exception

from core.hosted import is_hosted
from core.playwright_session import (
    PlaywrightSession,
    SessionConfig,
    SessionExpiredError,
    emit_phase as _emit,
    url_marker_login_check,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CALENDAR_URL = "https://vistasocial.com/calendar"
_LOGIN_URL = "https://vistasocial.com/login"
_LOGIN_URL_MARKERS = ("/login", "/signin", "/sign-in", "/oauth", "auth0")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SESSION_FILE = os.path.join(_PROJECT_ROOT, "vista_social_session.json")

# Network identifiers — used to find the right profile checkbox row.
# A row is identified by an <img src> ending in /<network>.svg.
_NETWORK_FACEBOOK = "facebook"
_NETWORK_INSTAGRAM = "instagram"
_NETWORK_YOUTUBE = "youtube"

_DEFAULT_TIMEOUT = 30_000      # 30 s
_UPLOAD_TIMEOUT = 600_000      # 10 min for video upload + processing

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session config — login/launch plumbing lives in core.playwright_session
# ---------------------------------------------------------------------------

_VS_SESSION_CONFIG = SessionConfig(
    name="vista_social",
    session_file=_SESSION_FILE,
    is_login_url=url_marker_login_check(_LOGIN_URL_MARKERS),
    target_url=_CALENDAR_URL,
    login_url=_LOGIN_URL,
    headless_env="VISTA_SOCIAL_HEADLESS",
    login_timeout_env="VISTA_SOCIAL_LOGIN_TIMEOUT",
    chrome_path_env="VISTA_SOCIAL_CHROME_PATH",
    viewport={"width": 1440, "height": 900},
    default_timeout_ms=_DEFAULT_TIMEOUT,
    no_login_recovery=is_hosted(),
)


# ---------------------------------------------------------------------------
# Composer flow
# ---------------------------------------------------------------------------

def _open_new_post(page) -> None:
    """Click Create → New post; wait for the Publish modal."""
    page.locator("button", has_text="Create").first.click()
    np = page.locator("button", has_text="New post").first
    np.wait_for(state="visible", timeout=_DEFAULT_TIMEOUT)
    np.click()
    # Caption textarea is a reliable signal that the Publish modal is mounted.
    page.wait_for_selector(
        "textarea[placeholder*='Write your content']",
        timeout=_DEFAULT_TIMEOUT,
    )


def _dismiss_autosave_prompt(page, timeout_ms: int = 8_000) -> bool:
    """Vista shows 'You have an auto saved post. Do you want to load it?'
    after a media attachment if a previous draft exists. The prompt can
    take a few seconds to render, so poll up to `timeout_ms`. Always
    start fresh so we don't inherit stale captions/profiles. Returns
    True iff a prompt was found and dismissed."""
    btn = page.locator("button", has_text="No, start fresh").first
    try:
        btn.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeout:
        # M5: ONLY treat genuine timeouts as "no prompt to dismiss". Any
        # other exception (TypeError, programming error) should propagate
        # so it isn't silently masked as "no autosave detected".
        return False
    try:
        btn.click()
        return True
    except PlaywrightTimeout:
        return False


def _set_profile_selection(page, networks_to_uncheck: list[str]) -> None:
    """Uncheck every profile row whose overlay is one of `networks_to_uncheck`.

    Vista's profile picker uses custom div-based checkboxes (no <input>),
    nested under `.Checkbox__Wrapper-sc-1at1571-0`. A row is "checked" iff
    it contains the checkmark <svg path d="M8.925 ...">. The picker can
    render the same logical row twice (sidebar list + an open dropdown
    panel above it), so we click ALL visible matches — clicking already-
    unchecked ones is a no-op for our purposes since both copies share
    the same React state and end up consistent after the click cascade.
    """
    if not networks_to_uncheck:
        return

    result = page.evaluate(
        """(networks) => {
            const wrappers = Array.from(
                document.querySelectorAll('.Checkbox__Wrapper-sc-1at1571-0')
            ).filter(el => el.offsetParent !== null);
            let clicked = 0;
            for (const w of wrappers) {
                const imgs = Array.from(w.querySelectorAll('img'));
                const matches = networks.some(net =>
                    imgs.some(img => (img.src || '').includes('/' + net + '.svg'))
                );
                if (!matches) continue;
                // Only click if currently checked.
                const isChecked = !!w.querySelector('svg path[d^="M8.925"]');
                if (!isChecked) continue;
                const target = w.querySelector('.Checkbox__StyledFlex-sc-1at1571-1') || w;
                target.click();
                clicked += 1;
            }
            return clicked;
        }""",
        networks_to_uncheck,
    )
    logger.info("Vista Social: unchecked %d profile row(s) for %s",
                result, networks_to_uncheck)


def _fill_caption(page, caption: str) -> None:
    """Fill the main 'Your post' textarea."""
    ta = page.locator("textarea[placeholder*='Write your content']").first
    ta.wait_for(state="visible", timeout=_DEFAULT_TIMEOUT)
    ta.fill(caption or "")


def _attach_media(page, file_path: str) -> None:
    """Attach a video file via Vista's hidden file input.

    Vista renders multiple hidden <input type=file> elements on the
    Publish modal — one image-only, one video-only, one all-media. We
    pick the first input whose `accept` lists `.mp4`, which is the
    video-or-mixed input. `set_input_files` works on hidden inputs.
    """
    inputs = page.locator("input[type=file]")
    count = inputs.count()
    if count == 0:
        raise RuntimeError("Vista Social: no file inputs found on Publish modal")

    chosen = None
    for i in range(count):
        accept = (inputs.nth(i).get_attribute("accept") or "").lower()
        if ".mp4" in accept:
            chosen = inputs.nth(i)
            break
    if chosen is None:
        chosen = inputs.first

    chosen.set_input_files(file_path)
    logger.info("Vista Social: attached media: %s", file_path)


def _wait_for_media_upload(page, timeout_ms: int) -> None:
    """Wait until Vista has ingested the attached file.

    Readiness signal: the composer renders an "Attached videos" header
    (or "Attached video" / "Attached image" depending on file type) in
    the main panel once Vista has registered the upload. The "Click to
    edit media" string in the right-side per-network panel is a
    persistent override placeholder, NOT a readiness indicator — keying
    off its disappearance was the original bug.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    last_state = None
    while time.time() < deadline:
        state = page.evaluate(
            r"""() => {
                const all = Array.from(document.querySelectorAll('*'))
                  .filter(el => el.offsetParent !== null);
                const matches = all.filter(el => /^Attached (videos?|images?|media)$/i.test((el.innerText || '').trim()));
                if (matches.length > 0) return 'attached';
                // Vista shows "Video processing" / "Processing" while the
                // server transcodes — treat as still-uploading but reachable.
                const processing = all.some(el =>
                    /\bprocessing\b/i.test((el.innerText || '').trim().slice(0, 60))
                );
                return processing ? 'processing' : 'idle';
            }"""
        )
        if state == "attached":
            return
        if state != last_state:
            logger.info("Vista Social: media state = %s", state)
            last_state = state
        page.wait_for_timeout(500)
    logger.warning(
        "Vista Social: 'Attached videos' header never appeared after %d s "
        "— proceeding anyway",
        timeout_ms // 1000,
    )


def _select_schedule_radio(page) -> None:
    """Click the bottom Schedule radio to switch from 'Publish now' mode."""
    # The label text 'Schedule' uniquely identifies the radio at the bottom.
    page.locator("label", has_text="Schedule").first.click()


def _click_next_to_schedule_step(page) -> None:
    """Advance the wizard to the date/time picker step and wait for the
    react-datepicker input to mount. If Vista didn't advance (e.g. an
    autosave modal intercepted the click, or media validation is still
    running), we retry once after dismissing any open dialog and after
    a short settle.
    """
    next_btn = page.locator("button", has_text="Next").first
    next_btn.wait_for(state="visible", timeout=_DEFAULT_TIMEOUT)
    next_btn.click()

    picker_sel = ".react-datepicker__input-container input"
    try:
        page.wait_for_selector(picker_sel, timeout=15_000)
        return
    except PlaywrightTimeout:
        pass

    # Retry path: dismiss any lingering modal (autosave / confirm), wait,
    # then click Next again.
    _dismiss_autosave_prompt(page, timeout_ms=2_000)
    page.wait_for_timeout(1_000)
    try:
        page.locator("button", has_text="Next").first.click()
    except Exception as e:
        # M6: log the retry failure so a stuck Next-button doesn't disappear
        # — the wait_for_selector below will surface a generic timeout that
        # would otherwise be hard to root-cause.
        logger.debug("Vista Social: Next-click retry failed: %s", e)
    page.wait_for_selector(picker_sel, timeout=30_000)


def _set_schedule_datetime(page, schedule_dt: datetime) -> None:
    """Set the post's scheduled datetime on the wizard's Schedule step.

    Vista's schedule step uses:
      * `react-datepicker` text input (placeholder/value formatted as
        ``Apr 29, 2026`` — i.e. ``%b %-d, %Y``).
      * Three native `<select>` elements named ``hours`` (01-12),
        ``minutes`` (00-59), ``interval`` (AM/PM).

    Vista displays times in the account's saved timezone (America/New_York
    for this account). We convert any tz-aware input to that zone before
    formatting.
    """
    target = schedule_dt
    try:
        from zoneinfo import ZoneInfo
        if target.tzinfo is not None:
            target = target.astimezone(ZoneInfo("America/New_York"))
    except Exception as e:  # noqa: BLE001 — fall back to the original datetime
        logger.debug("vista: tz conversion to America/New_York failed: %s", e)

    # Build platform-portable "Apr 29, 2026" without %-d (Linux-only).
    month_abbr = target.strftime("%b")
    date_str = f"{month_abbr} {target.day}, {target.year}"

    hour_12 = target.hour % 12
    if hour_12 == 0:
        hour_12 = 12
    hour_value = f"{hour_12:02d}"
    minute_value = f"{target.minute:02d}"
    interval_value = "PM" if target.hour >= 12 else "AM"

    # 1. Date — react-datepicker hooks onChangeRaw + onKeyDown, so a JS
    #    value setter alone updates the visible text but NOT the parsed
    #    Date kept in component state. Use real keystrokes and commit via
    #    Tab so the parser runs. Verified working against Vista's "Mmm d,
    #    YYYY" format (e.g. "Dec 1, 2026").
    picker_input = page.locator(".react-datepicker__input-container input").first
    picker_input.wait_for(state="visible", timeout=_DEFAULT_TIMEOUT)
    picker_input.click()
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.press("Delete")
    page.keyboard.type(date_str, delay=15)
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)

    visible_value = (picker_input.input_value() or "").strip()
    if visible_value.lower() != date_str.lower():
        raise RuntimeError(
            "Vista Social: date picker did not accept "
            f"{date_str!r} (input now shows {visible_value!r})"
        )

    # 2. Time — native selects, just use Playwright's select_option.
    page.select_option("select[name='hours']", value=hour_value)
    page.select_option("select[name='minutes']", value=minute_value)
    page.select_option("select[name='interval']", value=interval_value)
    page.wait_for_timeout(250)


def _click_schedule_confirm(page) -> None:
    """Click the final Schedule button on the wizard's last step.

    The bottom bar has a split button: a primary "Schedule" action + a
    small chevron that opens variants ("Schedule and assign"). Targeting
    `button:has-text("Schedule")` matches both, and the chevron-only
    button has *no* visible text but still matches because text-search
    looks at descendants. We pin the right button by requiring an
    enabled primary-style button whose innerText *equals* "Schedule".
    """
    # Wait up to ~10 s for Vista to validate the date+time and enable
    # the primary Schedule button.
    deadline_ticks = 40
    for _ in range(deadline_ticks):
        clicked = page.evaluate(
            """() => {
                const btns = Array.from(document.querySelectorAll('button'))
                  .filter(b => b.offsetParent && !b.disabled);
                // Prefer an exact "Schedule" textContent match — the
                // chevron sibling has empty innerText.
                const exact = btns.find(b => (b.innerText || '').trim() === 'Schedule');
                if (!exact) return false;
                exact.click();
                return true;
            }"""
        )
        if clicked:
            return
        page.wait_for_timeout(250)
    raise RuntimeError(
        "Vista Social: Schedule button never enabled — date/time may "
        "not have been accepted."
    )


def _confirm_schedule_committed(page, timeout_ms: int = 30_000) -> None:
    """After clicking Schedule, verify Vista accepted the post.

    Two outcomes:
      * Success — the Publish modal detaches (textarea gone) and we may
        see a "scheduled" toast briefly.
      * Failure — Vista keeps the modal open and renders an inline error
        (e.g. "Instagram requires a square aspect ratio"). We surface
        any visible alert/error text rather than silently returning OK.
    """
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        state = page.evaluate(
            r"""() => {
                const ta = document.querySelector("textarea[placeholder*='Write your content']");
                const modalGone = !ta || ta.offsetParent === null;
                // Scrape any visible alert/error nodes.
                const errs = Array.from(document.querySelectorAll(
                    "[class*='Alert'],[class*='Error'],[class*='error'],[role='alert']"
                ))
                  .filter(el => el.offsetParent !== null)
                  .map(el => (el.innerText || '').trim())
                  .filter(t => t.length > 4 && t.length < 400);
                return { modalGone, errs };
            }"""
        )
        if state.get("modalGone"):
            return
        errs = state.get("errs") or []
        # Surface the first non-trivial error message we see.
        for msg in errs:
            if any(kw in msg.lower() for kw in (
                "fail", "error", "required", "must", "invalid", "cannot"
            )):
                raise RuntimeError(f"Vista Social refused the post: {msg}")
        page.wait_for_timeout(500)
    raise RuntimeError(
        "Vista Social: Publish modal never closed after clicking Schedule "
        f"(waited {timeout_ms // 1000}s). Post likely was not scheduled."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_post(entry, elements=None, progress_callback=None) -> dict:
    """Schedule a Vista Social post (Instagram + Facebook) for `entry`.

    The video used is `entry.youtube_shorts_path` (same file as the
    YouTube Shorts upload). The caption uses, in order of preference:
        entry.vista_caption (if set)
        entry.description (the YouTube video description)

    Progress phases via progress_callback:
        "launching", "awaiting_login" (first-run only), "navigating",
        "configuring_profiles", "filling_caption", "uploading_media",
        "scheduling", "done"

    Returns: {"success": bool, "url": str|None, "skipped": bool, "error": str|None}
    """
    if elements is not None and not getattr(elements, "vs_enabled", True):
        return {"success": True, "skipped": True, "url": None}

    file_path = getattr(entry, "youtube_shorts_path", None)
    if not file_path or not os.path.isfile(file_path):
        return {"success": False, "error": f"Shorts file not found: {file_path}"}

    vs_caption = elements is None or getattr(elements, "vs_description", True)
    vs_schedule = elements is None or getattr(elements, "vs_schedule", True)

    caption_source = (
        getattr(entry, "vista_caption", "") or getattr(entry, "description", "") or ""
    ) if vs_caption else ""
    caption = caption_source.strip()
    if vs_caption:
        try:
            from core.config import load_config
            footer = (load_config().get("description_footers", {}).get("vista_social", "") or "").strip()
        except Exception as e:
            # M6: log so a missing/broken config is diagnosable rather than
            # silently producing footer-less captions.
            logger.warning("Vista Social: failed to load footer from config: %s", e)
            footer = ""
        if footer:
            caption = f"{caption}\n\n{footer}" if caption else footer

    schedule_dt = (
        getattr(entry, "vista_schedule_dt", None) if vs_schedule else None
    )
    if vs_schedule and schedule_dt is None:
        return {
            "success": False,
            "error": "Vista Social: a schedule datetime is required (this "
                     "uploader does not publish immediately)",
        }

    # Per the user's spec: only Instagram + Facebook should remain selected.
    networks_to_uncheck = [_NETWORK_YOUTUBE]

    try:
        with PlaywrightSession(_VS_SESSION_CONFIG, progress_callback=progress_callback) as sess:
            page = sess.page
            assert page is not None

            _emit(progress_callback, "navigating")
            _open_new_post(page)

            # Vista shows "You have an auto saved post. Do you want to
            # load it?" the moment the Publish modal mounts if a prior
            # draft exists. Clicking "No, start fresh" resets the WHOLE
            # form — including any media attachment. So we must dismiss
            # this BEFORE doing any setup; trying to dismiss it after
            # uploading media wipes the upload and the rest of the flow
            # waits forever for an "Attached videos" header that never
            # comes back.
            _dismiss_autosave_prompt(page)

            _emit(progress_callback, "configuring_profiles")
            _set_profile_selection(page, networks_to_uncheck)

            _emit(progress_callback, "filling_caption")
            _fill_caption(page, caption)

            _emit(progress_callback, "uploading_media")
            _attach_media(page, file_path)
            _wait_for_media_upload(page, _UPLOAD_TIMEOUT)

            _emit(progress_callback, "scheduling")
            _select_schedule_radio(page)
            _click_next_to_schedule_step(page)
            _set_schedule_datetime(page, schedule_dt)
            _click_schedule_confirm(page)
            _confirm_schedule_committed(page)

            _emit(progress_callback, "done")
            # Vista's calendar URL is the best landing page we can offer —
            # the dashboard does not surface a stable per-post permalink in
            # the URL after Schedule (it's a calendar tile, not a route).
            return {"success": True, "url": page.url or _CALENDAR_URL}

    except SessionExpiredError:
        # Hosted mode: propagate so the orchestrator surfaces the re-Connect
        # message rather than the generic RuntimeError branch below.
        raise
    except PlaywrightTimeout as exc:
        return {"success": False, "error": f"Vista Social timed out: {exc}"}
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("Vista Social: unexpected error")
        return {"success": False, "error": str(exc)}
