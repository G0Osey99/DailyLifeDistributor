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
# Safety cap on the media-ready wait. With the processing→settled heuristic in
# _wait_for_media_upload this rarely bites; it only matters if Vista never
# shows a processing indicator AND our "Attached" header matcher misses. Was
# 10 min, which left the agent hung for the full duration on a header-text
# mismatch; 5 min (env-tunable) is plenty for a Shorts upload + transcode.
_UPLOAD_TIMEOUT = int(os.environ.get("VISTA_MEDIA_TIMEOUT_MS", "300000"))
# How long to keep re-clicking "Next" while a connected network (Instagram)
# is still validating the just-uploaded video. The operator confirms IG
# accepts these Shorts videos when posted by hand, so a content-validation
# toast here means "video not finished processing yet", not "rejected" —
# we retry until the date picker mounts or this budget is spent.
_SCHEDULE_ADVANCE_TIMEOUT_S = int(os.environ.get("VISTA_SCHEDULE_ADVANCE_TIMEOUT", "180"))

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
# Diagnostics
# ---------------------------------------------------------------------------

def _vista_debug_dir() -> str:
    """Directory for schedule-step DOM dumps, picked for the runtime context:

      * hosted VPS web path -> /data/vista-debug (persistent volume, readable
        over SSH);
      * hybrid agent -> ~/.dld-agent/vista-debug, next to the agent's logs +
        config. Crucially NOT the repo-relative fallback: in the frozen
        PyInstaller agent that path is inside the bundle's temp extraction dir
        (sys._MEIPASS), which is wiped on exit, so the snapshot would be lost;
      * dev / USB Flask -> repo-local .vista-debug.
    """
    if os.path.isdir("/data"):
        base = "/data/vista-debug"
    else:
        agent_home = os.path.join(os.path.expanduser("~"), ".dld-agent")
        base = (os.path.join(agent_home, "vista-debug") if os.path.isdir(agent_home)
                else os.path.join(_PROJECT_ROOT, ".vista-debug"))
    os.makedirs(base, exist_ok=True)
    return base


def _capture_debug(page, label: str) -> str:
    """Dump a screenshot + HTML + a compact triage JSON of the current page.

    Best-effort — never raises (a failing capture must not mask the real
    error). Returns the directory written, or "" on total failure. Used to
    root-cause the Vista schedule step blind, where we can't see the live DOM:
    one re-run leaves a full snapshot on disk we can read back.
    """
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = os.path.join(_vista_debug_dir(), f"{label}-{stamp}")
        os.makedirs(out, exist_ok=True)
        try:
            page.screenshot(path=os.path.join(out, "screenshot.png"), full_page=True)
        except Exception as e:  # noqa: BLE001
            logger.debug("vista debug screenshot failed: %s", e)
        try:
            with open(os.path.join(out, "page.html"), "w", encoding="utf-8") as fh:
                fh.write(page.content())
        except Exception as e:  # noqa: BLE001
            logger.debug("vista debug html failed: %s", e)
        # Compact triage: URL, every enabled+visible button's text, whether a
        # react-datepicker input is in the DOM, and which "Schedule" controls
        # look selected. This single JSON usually pinpoints the root cause
        # (radio not toggled vs. inline picker vs. disabled Next).
        try:
            info = page.evaluate(
                """() => {
                    const vis = (el) => !!el.offsetParent;
                    const btns = Array.from(document.querySelectorAll('button'))
                        .filter(vis)
                        .map(b => ({text: (b.innerText||'').trim(), disabled: b.disabled}));
                    const pickers = document.querySelectorAll(
                        '.react-datepicker__input-container input');
                    const dateInputs = Array.from(
                        document.querySelectorAll('input'))
                        .filter(i => /date|schedule|datepicker/i.test(
                            (i.className||'') + ' ' + (i.name||'') + ' ' +
                            (i.placeholder||'')))
                        .map(i => ({name: i.name, cls: i.className,
                                    ph: i.placeholder, type: i.type}));
                    const checked = Array.from(
                        document.querySelectorAll('input[type=radio],input[type=checkbox]'))
                        .filter(i => i.checked)
                        .map(i => ({name: i.name, value: i.value,
                                    aria: i.getAttribute('aria-label')}));
                    return {url: location.href,
                            buttons: btns,
                            react_datepicker_count: pickers.length,
                            date_like_inputs: dateInputs,
                            checked_inputs: checked};
                }"""
            )
            import json as _json
            with open(os.path.join(out, "info.json"), "w", encoding="utf-8") as fh:
                _json.dump(info, fh, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.debug("vista debug triage failed: %s", e)
        logger.error("Vista Social: saved schedule-step diagnostics to %s", out)
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("vista debug capture failed entirely: %s", e)
        return ""


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


def _set_profile_selection(page, networks_to_check: list[str],
                           networks_to_uncheck: list[str]) -> None:
    """Force each target network's profile row into the desired checked state.

    Vista remembers the last-used profile selection PER SESSION, so the
    automation's stored session can default to a different set than the
    operator sees interactively (observed: the automation defaulted to
    Facebook-only, so Instagram silently dropped). We therefore *ensure*
    Facebook+Instagram are checked and YouTube unchecked, rather than relying
    on the default — the old "only uncheck YouTube" approach left IG off.

    Picker mechanics (confirmed via scripts/vista_schedule_recon.py): rows are
    `.Checkbox__Wrapper-sc-1at1571-0` with an `<img src=".../<network>.svg">`;
    a row is checked iff it has the checkmark `svg path[d^="M8.925"]`. The same
    logical row renders twice (sidebar list + open dropdown) and both copies
    share React state, so we toggle each network at most ONCE (dedupe by
    network) — clicking both copies would toggle the shared state twice and
    cancel out.
    """
    result = page.evaluate(
        """(args) => {
            const toCheck = args.toCheck, toUncheck = args.toUncheck;
            const wrappers = Array.from(
                document.querySelectorAll('.Checkbox__Wrapper-sc-1at1571-0')
            ).filter(el => el.offsetParent !== null);
            const seen = new Set();
            const changed = [];
            for (const w of wrappers) {
                const imgs = Array.from(w.querySelectorAll('img'))
                    .map(i => i.src || '');
                const netOf = (list) => list.find(n =>
                    imgs.some(s => s.includes('/' + n + '.svg')));
                const net = netOf(toCheck) || netOf(toUncheck);
                if (!net || seen.has(net)) continue;  // dedupe + ignore others
                seen.add(net);
                const want = toCheck.includes(net);
                const isChecked = !!w.querySelector('svg path[d^="M8.925"]');
                if (isChecked === want) continue;
                (w.querySelector('.Checkbox__StyledFlex-sc-1at1571-1') || w).click();
                changed.push((want ? '+' : '-') + net);
            }
            return changed;
        }""",
        {"toCheck": networks_to_check, "toUncheck": networks_to_uncheck},
    )
    logger.info("Vista Social: profile selection adjusted: %s (want +%s -%s)",
                result or "none", networks_to_check, networks_to_uncheck)


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
    the main panel once Vista has registered the upload.

    Two persistent strings on this panel are NOT readiness signals and must
    not be keyed off:
      * "Click to edit media" — a per-network override placeholder.
      * "Video processing." / "Image processing." — feature labels (each
        with a Learn-more link), always present once media is attached. An
        earlier attempt waited while ANY element read "processing", which
        matched these permanent labels and wedged the wait for the full
        timeout even though the video was already attached. We gate ONLY on
        the "Attached …" header; any genuinely not-yet-finalized media is
        caught downstream by the schedule step's retry-Next loop, which
        re-clicks until the date picker mounts.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    announced_wait = False
    while time.time() < deadline:
        attached = page.evaluate(
            r"""() => {
                const all = Array.from(document.querySelectorAll('*'))
                  .filter(el => el.offsetParent !== null);
                // Allow a trailing count/suffix (e.g. "Attached videos (1)").
                return all.some(el =>
                    /^Attached (videos?|images?|media)\b/i.test((el.innerText || '').trim()));
            }"""
        )
        if attached:
            logger.info("Vista Social: media attached")
            return
        if not announced_wait:
            logger.info("Vista Social: waiting for media to attach")
            announced_wait = True
        page.wait_for_timeout(500)
    # Timed out: snapshot the media panel so we can see why the "Attached"
    # header never appeared.
    _capture_debug(page, "media-wait-timeout")  # logs the snapshot dir it wrote
    logger.warning(
        "Vista Social: 'Attached …' header never appeared after %d s "
        "— proceeding anyway (DOM snapshot saved; see the path logged above)",
        timeout_ms // 1000,
    )


def _select_schedule_radio(page) -> None:
    """Click the bottom Schedule radio to switch from 'Publish now' mode."""
    # The label text 'Schedule' uniquely identifies the radio at the bottom.
    page.locator("label", has_text="Schedule").first.click()


_PICKER_SEL = ".react-datepicker__input-container input"


def _detect_network_validation_error(page) -> str:
    """Return Vista's content-validation toast text if it's showing, else "".

    When a connected network rejects the post's media (e.g. Instagram won't
    accept the Shorts video's length/aspect/format), Vista shows a toast like
    "Please check your content on the following social networks: Instagram"
    and refuses to advance past Next. Detecting it lets us raise an actionable
    error instead of blindly waiting out the date-picker timeout. Confirmed
    against the live DOM via scripts/vista_schedule_recon.py.
    """
    try:
        return (page.evaluate(
            r"""() => {
                const els = Array.from(document.querySelectorAll('*'))
                  .filter(el => el.offsetParent !== null);
                for (const el of els) {
                    const t = (el.innerText || '').trim();
                    if (!t || t.length > 220) continue;
                    if (/check your content on the following social network/i.test(t)
                        || /can.?t be (scheduled|published|posted)/i.test(t)) {
                        return t;
                    }
                }
                return '';
            }"""
        ) or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.debug("Vista Social: validation-toast probe failed: %s", e)
        return ""


def _picker_visible(page) -> bool:
    picker = page.locator(_PICKER_SEL)
    return bool(picker.count()) and picker.first.is_visible()


def _click_next_to_schedule_step(page) -> None:
    """Advance the wizard to the date/time picker step.

    Vista blocks "Next" with a content-validation toast ("check your content
    on the following social networks: Instagram") while the just-uploaded
    Shorts video is still being processed/validated for a connected network.
    The operator confirms Instagram *does* accept these videos when posted by
    hand, so that toast means "not finished yet", not "rejected". So we keep
    re-clicking Next (dismissing the toast / any autosave modal between
    attempts) until the date picker mounts — mirroring the human flow of
    waiting for the upload to settle before scheduling — up to
    ``_SCHEDULE_ADVANCE_TIMEOUT_S``. Only if that whole budget is spent do we
    surface an actionable error (run() also captures a DOM snapshot).
    """
    deadline = time.time() + _SCHEDULE_ADVANCE_TIMEOUT_S
    attempt = 0
    last_toast = ""
    while time.time() < deadline:
        attempt += 1
        _dismiss_autosave_prompt(page, timeout_ms=1_000)
        try:
            nb = page.locator("button", has_text="Next").first
            nb.wait_for(state="visible", timeout=_DEFAULT_TIMEOUT)
            nb.click()
        except Exception as e:  # noqa: BLE001
            logger.debug("Vista Social: Next click (attempt %d) failed: %s", attempt, e)

        # Give the picker a chance to mount after this click.
        picker_deadline = time.time() + 8.0
        while time.time() < picker_deadline:
            if _picker_visible(page):
                if attempt > 1:
                    logger.info("Vista Social: advanced to schedule step on attempt %d", attempt)
                return
            page.wait_for_timeout(400)

        toast = _detect_network_validation_error(page)
        if toast:
            last_toast = toast
            logger.info(
                "Vista Social: 'Next' blocked while media validates (attempt %d): "
                "%s — waiting, will retry", attempt, toast,
            )
            page.wait_for_timeout(5_000)  # let the video finish processing
        else:
            page.wait_for_timeout(2_000)  # transient: re-click shortly

    if last_toast:
        raise RuntimeError(
            "Vista Social: could not reach the schedule step within "
            f"{_SCHEDULE_ADVANCE_TIMEOUT_S}s — a connected network kept blocking "
            f"\"Next\": \"{last_toast}\". The Shorts video likely never finished "
            "uploading/processing in Vista."
        )
    # No toast ever seen either — surface the explicit picker timeout.
    page.wait_for_selector(_PICKER_SEL, timeout=5_000)


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
        if not file_path:
            return {"success": False, "error": (
                "Vista Social needs the Shorts video (it posts the same clip), "
                "but no file was matched in the 'Vertical Video (Shorts)' folder "
                "for this date. Add the Shorts file or uncheck Vista Social."
            )}
        return {"success": False, "error": f"Shorts file not found on disk: {file_path}"}

    vs_caption = elements is None or getattr(elements, "vs_description", True)
    vs_schedule = elements is None or getattr(elements, "vs_schedule", True)

    caption_source = (
        getattr(entry, "vista_caption", "") or getattr(entry, "description", "") or ""
    ) if vs_caption else ""
    caption = caption_source.strip()
    if vs_caption:
        # Prefer the per-org footer baked into the entry at build time;
        # fall back to load_config() for entries built by older paths.
        entry_footer = getattr(entry, "vista_social_description_footer", None)
        if entry_footer is not None:
            footer = (entry_footer or "").strip()
        else:
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

    # Per the user's spec: only Instagram + Facebook should be posted to.
    # Vista's per-session default can drift (the automation's session defaulted
    # to Facebook-only), so we actively CHECK both and uncheck YouTube rather
    # than trusting the default.
    networks_to_check = [_NETWORK_FACEBOOK, _NETWORK_INSTAGRAM]
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
            _set_profile_selection(page, networks_to_check, networks_to_uncheck)

            _emit(progress_callback, "filling_caption")
            _fill_caption(page, caption)

            _emit(progress_callback, "uploading_media")
            _attach_media(page, file_path)
            _wait_for_media_upload(page, _UPLOAD_TIMEOUT)

            _emit(progress_callback, "scheduling")
            try:
                _select_schedule_radio(page)
                _click_next_to_schedule_step(page)
                _set_schedule_datetime(page, schedule_dt)
                _click_schedule_confirm(page)
                _confirm_schedule_committed(page)
            except Exception:
                # On any schedule-step failure, leave a DOM + screenshot
                # snapshot (under /data/vista-debug on the VPS) so the exact
                # blocker is recoverable instead of a blind timeout.
                _capture_debug(page, "schedule-fail")
                raise

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
