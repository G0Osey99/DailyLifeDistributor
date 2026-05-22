"""SimpleCast uploader — drives the dashboard via Playwright.

Session/launch plumbing lives in :mod:`core.playwright_session`; this
module is now just SimpleCast-specific selectors and form flow.

* FIRST RUN (or after the session expires) — a Chrome window opens to
  SimpleCast's login page and waits for the user to log in manually.
  On success, cookies + local storage are saved to `simplecast_session.json`
  at the project root.

* EVERY RUN AFTER THAT — the saved session is loaded and the upload
  proceeds without any login step.

REQUIREMENTS:
    pip install playwright   (no `playwright install` step needed)

Environment variables (all optional):
    SIMPLECAST_UPLOAD_URL     Override the show-scoped new-episode URL
    SIMPLECAST_HEADLESS       "true" to hide the automation window once
                              a session is cached. First-time login is
                              always headed regardless. Default: false
    SIMPLECAST_LOGIN_TIMEOUT  Seconds to wait for the user to log in on
                              first run. Default: 300 (5 minutes)
    SIMPLECAST_CHROME_PATH    Full path to Chrome binary if it's in a
                              non-standard location
"""

import logging
import os
import re
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

# Show-scoped new-episode URL for the Daily Life show.
# Override via SIMPLECAST_UPLOAD_URL env var if the show ever changes.
_DEFAULT_UPLOAD_URL = (
    "https://dashboard.simplecast.com/accounts/"
    "fbbdf431-bf12-4f72-acd7-0fb0bdd6c798/shows/"
    "e64f6974-8126-44da-92e3-4ead66d25d01/episodes/new"
)

# Where the saved session lives (on the USB, next to the rest of the app).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SESSION_FILE = os.path.join(_PROJECT_ROOT, "simplecast_session.json")

# Substrings that indicate we're sitting on a login / auth screen.
_LOGIN_URL_MARKERS = ("/login", "/signin", "/sign-in",
                       "accounts.google.com", "auth0.com", "/oauth")

# ---- Selectors on the new-episode form ----
_SEL_TITLE        = "#form-input-title"
_SEL_DESCRIPTION  = "#form-input-description"            # Episode Summary textarea
_SEL_NOTES_EDITOR = ".ck-editor__editable"                # CKEditor 5 contenteditable
_SEL_BROWSE_LINK  = ".drag-n-drop .browse"                # "click to Browse." span
_SEL_AUDIO_INPUT  = "input[type='file']"                  # hidden file input
_SEL_SAVE_BUTTON  = "button:has(span:text-is('Save'))"

# ---- Selectors on the scheduling (draft) page ----
# The whole picker widget. Starts with class "disabled" until the page is
# ready for scheduling; we wait for that class to drop before opening it.
_SEL_PICKER_ROOT     = ".timeframe-picker"
# The clickable area that opens the date/time panel.
_SEL_PICKER_SELECTOR = ".timeframe-picker .selector"
# The panel that slides open after clicking. `display: none` until opened.
_SEL_PICKER_PANEL    = ".timeframe-picker .options-panel"
# Header showing "April 2026" etc., inside the open panel.
_SEL_PICKER_TITLE    = ".timeframe-picker .vc-title"
# Prev / next month arrows.
_SEL_PICKER_PREV     = ".timeframe-picker .vc-arrow.is-left"
_SEL_PICKER_NEXT     = ".timeframe-picker .vc-arrow.is-right"
# The three custom dropdowns for hour, minute, and AM/PM, in that order.
_SEL_TIME_DROPDOWNS  = ".timeframe-picker .picker-time .wrap"
# The Schedule button in the RSS card to the right of the picker.
_SEL_SCHEDULE_BUTTON = "button.button-save"

# ---- Timeouts ----
_DEFAULT_TIMEOUT = 30_000      # 30 s for normal actions
_UPLOAD_TIMEOUT  = 600_000     # 10 min for audio upload/encoding

# Episode UUID from the post-save URL, e.g.
#   /episodes/abc12345-... /edit  → "abc12345-..."
_EPISODE_ID_RE = re.compile(
    r"/episodes/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


def _podcast_description_footer() -> str:
    """Read description_footers.podcast from config.yaml. Returns '' on any failure."""
    try:
        from core.config import load_config
        cfg = load_config() or {}
        return (cfg.get("description_footers", {}).get("podcast", "") or "").strip()
    except Exception as exc:
        logger.debug("Could not read podcast footer from config.yaml: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Form helpers
# ---------------------------------------------------------------------------

def _fill_title(page, title):
    page.wait_for_selector(_SEL_TITLE, timeout=_DEFAULT_TIMEOUT)
    page.fill(_SEL_TITLE, title)


def _fill_description(page, description):
    page.fill(_SEL_DESCRIPTION, description)


def _fill_notes(page, notes_text):
    """Fill the CKEditor 5 Episode Notes field by focusing and typing.

    Setting innerHTML directly tends to desync CKEditor's internal model,
    so we send keystrokes through the editor's normal input pipeline.
    """
    editor = page.locator(_SEL_NOTES_EDITOR).first
    editor.wait_for(state="visible", timeout=_DEFAULT_TIMEOUT)
    editor.click()
    try:
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.press("Delete")
    except Exception as e:
        # M2: keystroke-clear failure means the field still holds prior
        # content; typing would APPEND. Log and verify before continuing.
        logger.warning("SimpleCast: notes-clear keystrokes failed: %s", e)
    # Verify the editor is actually empty before typing — if not, fall back
    # to clearing via JS so we don't publish stale notes prepended to new.
    try:
        existing = (editor.inner_text(timeout=2000) or "").strip()
    except Exception:
        existing = ""
    if existing:
        logger.warning("SimpleCast: notes editor not cleared by keystrokes; clearing via JS")
        try:
            editor.evaluate("el => { el.innerHTML = ''; el.dispatchEvent(new Event('input', {bubbles:true})); }")
        except Exception as e:
            logger.warning("SimpleCast: JS notes-clear also failed: %s — proceeding (notes may be appended)", e)
    page.keyboard.type(notes_text, delay=1)


def _upload_audio(page, file_path):
    """Attach audio via hidden file input, falling back to the Browse link."""
    inputs = page.locator(_SEL_AUDIO_INPUT)
    if inputs.count() > 0:
        inputs.first.set_input_files(file_path)
        return
    with page.expect_file_chooser() as fc_info:
        page.locator(_SEL_BROWSE_LINK).first.click()
    fc_info.value.set_files(file_path)


def _wait_for_audio_ready(page, timeout):
    """Wait for the drag-drop prompt to collapse into the uploaded-file UI."""
    try:
        page.locator(_SEL_BROWSE_LINK).first.wait_for(
            state="detached", timeout=timeout
        )
    except PlaywrightTimeout:
        logger.warning(
            "SimpleCast: drag-drop prompt still present after %d s", timeout // 1000
        )


def _click_save(page):
    """Click Save and wait for the URL to move off /episodes/new."""
    btn = page.locator(_SEL_SAVE_BUTTON).first
    btn.wait_for(state="visible", timeout=_DEFAULT_TIMEOUT)

    # Save is disabled until audio processing finishes.
    deadline_ticks = _UPLOAD_TIMEOUT // 500
    for _ in range(deadline_ticks):
        if btn.is_enabled():
            break
        page.wait_for_timeout(500)
    else:
        raise RuntimeError("Save button never became enabled — audio upload stalled?")

    btn.click()
    page.wait_for_url(
        lambda url: "/episodes/new" not in url and "/episodes/" in url,
        timeout=_DEFAULT_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
# The date picker is a v-calendar widget wrapped in a custom <.timeframe-picker>.
# Key quirks:
#   * The whole picker carries class "disabled" until the page is ready.
#   * Day cells have stable ids like "id-2026-04-23" on the <.vc-day> div,
#     so we target those directly instead of matching on visible day text
#     (which would collide between adjacent months).
#   * The three time fields (hour, minute, AM/PM) are custom <li>-based
#     dropdowns, not <select>s. Each option has a `value` attribute we
#     can match on — hour uses "00"-"11" where "00" displays as 12,
#     minutes are in 5-minute increments, and AM/PM uses "am"/"pm".
# ---------------------------------------------------------------------------

def _compute_schedule_targets(schedule_dt) -> dict:
    """Pure helper: derive every selector value the date picker needs.

    Split out from :func:`_schedule_episode` so the timezone math, hour
    mod-12, 5-minute snap, and aria-label construction can be unit-tested
    without launching a browser. Returns a dict with:
      * ``target`` — the datetime, converted to America/New_York if tz-aware
      * ``day_id`` — ``id-YYYY-MM-DD`` class fragment for ``.vc-day``
      * ``aria`` — full aria-label SimpleCast renders on the day span
      * ``header`` — ``"%B %Y"`` text shown at the picker top (cased
        normally; the picker styles it to uppercase via CSS)
      * ``hour_value`` — ``"00".."11"`` string for the hour ``<li>``
      * ``minute_value`` — minute snapped to nearest 5 ("00".."55")
      * ``ampm_value`` — ``"am"`` or ``"pm"``
    """
    target = schedule_dt
    try:
        from zoneinfo import ZoneInfo
        if target.tzinfo is not None:
            target = target.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        logger.warning(
            "tz conversion to America/New_York failed for %r; using value as-is",
            schedule_dt, exc_info=True,
        )

    # The widget renders day cells with two handy hooks:
    #   * <div class="vc-day id-2026-05-13 ...">
    #   * <span aria-label="Wednesday, May 13, 2026" role="button" ...>
    # Building the aria-label manually (rather than strftime("%-d")) keeps
    # this working on both Unix and Windows.
    return {
        "target":       target,
        "day_id":       f"id-{target.year:04d}-{target.month:02d}-{target.day:02d}",
        "aria":         (f"{target.strftime('%A')}, {target.strftime('%B')} "
                         f"{target.day}, {target.year}"),
        "header":       f"{target.strftime('%B')} {target.year}",
        "hour_value":   f"{target.hour % 12:02d}",
        "minute_value": f"{(round(target.minute / 5) * 5) % 60:02d}",
        "ampm_value":   "pm" if target.hour >= 12 else "am",
    }


def _parse_picker_header(text: str) -> datetime:
    """Parse the picker header (e.g. ``"MAY 2026"``) into a datetime.

    The header is uppercased via CSS, so ``inner_text()`` returns ``"MAY 2026"``
    even though the DOM contains ``"May 2026"``. ``%B`` parsing is locale-
    sensitive and case-sensitive on some platforms, so we title-case before
    parsing. Raises ``ValueError`` on unexpected formats so the caller can
    surface a clear error.
    """
    return datetime.strptime(text.strip().title(), "%B %Y")


def _compute_month_delta(current_text: str, target_dt: datetime) -> int:
    """Return signed month delta from the picker's current header to target.

    Positive = click the next-month arrow; negative = click prev-month.
    """
    current_dt = _parse_picker_header(current_text)
    return ((target_dt.year - current_dt.year) * 12
            + (target_dt.month - current_dt.month))


def _schedule_episode(page, schedule_dt):
    """Open the date picker, set the datetime, and click Schedule."""
    sched = _compute_schedule_targets(schedule_dt)
    target         = sched["target"]
    target_day_id  = sched["day_id"]
    target_aria    = sched["aria"]
    target_header  = sched["header"]
    hour_value     = sched["hour_value"]
    minute_value   = sched["minute_value"]
    ampm_value     = sched["ampm_value"]

    # 1. Open the picker. The root element carries a `disabled` class as its
    #    default idle state — that is NOT a readiness signal, so we don't
    #    wait for it to clear. We just wait for the widget to be visible,
    #    then click. If the click doesn't open the panel, the panel's
    #    wait_for(visible) below will surface a clear error.
    picker = page.locator(_SEL_PICKER_ROOT).first
    picker.wait_for(state="visible", timeout=_DEFAULT_TIMEOUT)

    page.locator(_SEL_PICKER_SELECTOR).first.click()
    page.locator(_SEL_PICKER_PANEL).first.wait_for(
        state="visible", timeout=_DEFAULT_TIMEOUT
    )
    # Give Vue a beat to finish rendering the initial month grid.
    page.wait_for_timeout(350)

    # 2. Compute the month delta and click the arrow exactly that many times.
    #    This is more reliable than a read-and-click loop — the earlier
    #    version could misbehave on fast machines because inner_text() can
    #    return stale content if the Vue re-render hasn't finished.
    #
    #    Note: the title is styled with text-transform: uppercase, so
    #    inner_text() returns e.g. "MAY 2026" even though the DOM contains
    #    "May 2026". We compare case-insensitively and rely on strptime's
    #    case-insensitive %B parsing.
    title_loc = page.locator(_SEL_PICKER_TITLE).first
    current_text = title_loc.inner_text(timeout=5_000).strip()
    try:
        months_delta = _compute_month_delta(current_text, target)
    except ValueError:
        raise RuntimeError(
            f"Unexpected date picker header format: {current_text!r}"
        )
    if abs(months_delta) > 120:  # 10 years — almost certainly a bug, not a real schedule
        raise RuntimeError(
            f"Target date is {months_delta} months from the current picker "
            f"view ({current_text}); refusing to click that many arrows."
        )

    arrow_sel = _SEL_PICKER_NEXT if months_delta > 0 else _SEL_PICKER_PREV
    arrow_loc = page.locator(arrow_sel).first
    for _ in range(abs(months_delta)):
        arrow_loc.click()
        # Small delay lets the fade/slide transition and Vue re-render settle
        # before the next click so we don't miss or duplicate events.
        page.wait_for_timeout(180)

    # 3. Final render settle, then sanity-check the header. If it's wrong,
    #    surface both the expected and actual values so debugging is easy.
    page.wait_for_timeout(250)
    final_text = title_loc.inner_text(timeout=5_000).strip()
    if final_text.lower() != target_header.lower():
        raise RuntimeError(
            f"Month navigation ended at {final_text!r}, expected {target_header!r}"
        )

    # 4. Click the day. Try the id-YYYY-MM-DD class first (most specific);
    #    if that somehow doesn't match, fall back to the aria-label which
    #    the widget is guaranteed to render on the <span>.
    day_by_id   = page.locator(f".vc-day.{target_day_id} .vc-day-content")
    day_by_aria = page.locator(f"span[aria-label='{target_aria}']")
    if day_by_id.count() > 0:
        day_by_id.first.click()
    elif day_by_aria.count() > 0:
        day_by_aria.first.click()
    else:
        raise RuntimeError(
            f"Could not find day cell for {target_aria} in the picker. "
            f"Header reads {final_text!r}."
        )

    # 5. Set time via the three <li value="..."> dropdowns.
    _set_time_dropdown(page, 0, hour_value)    # hour
    _set_time_dropdown(page, 1, minute_value)  # minute
    _set_time_dropdown(page, 2, ampm_value)    # am/pm

    # 6. Close the picker so it doesn't intercept the Schedule click.
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)

    # 7. Click the Schedule button. It's disabled until a valid datetime is set.
    schedule_btn = page.locator(_SEL_SCHEDULE_BUTTON).first
    for _ in range(40):   # up to 10 s for validation to enable it
        if schedule_btn.is_enabled():
            break
        page.wait_for_timeout(250)
    else:
        raise RuntimeError(
            "Schedule button never became enabled — date/time may not "
            "have registered. Episode is saved as a draft."
        )
    schedule_btn.click()

    # 8. A confirmation dialog appears ("Are you sure you want to schedule
    #    this episode?"). Click Yes and wait for the dialog to disappear.
    #    The dialog is rendered as an overlay; we match the button by its
    #    visible text. Scoping to a visible, enabled button avoids false
    #    matches against hidden / stale DOM nodes.
    yes_btn = page.get_by_role("button", name="Yes").filter(visible=True).first
    try:
        yes_btn.wait_for(state="visible", timeout=10_000)
        yes_btn.click()
        # Dialog should close once the schedule request succeeds.
        yes_btn.wait_for(state="hidden", timeout=_DEFAULT_TIMEOUT)
    except PlaywrightTimeout:
        # If we didn't find a confirm dialog, Simplecast may have changed
        # the flow — log it but don't treat it as fatal, since the
        # Schedule click itself may already have succeeded.
        logger.warning(
            "SimpleCast: no confirmation dialog appeared after clicking "
            "Schedule — verify the episode state manually."
        )

    page.wait_for_load_state("networkidle", timeout=_DEFAULT_TIMEOUT)


def _set_time_dropdown(page, position, value):
    """Open the Nth time dropdown in the picker and click the option with
    the given `value` attribute.

    `position` is 0 (hour), 1 (minute), or 2 (AM/PM).
    """
    wrap = page.locator(_SEL_TIME_DROPDOWNS).nth(position)
    wrap.click()
    # Scope to this dropdown's option list so we don't hit an identically-
    # valued option in a different dropdown (e.g. "00" exists in both
    # hour and minute lists).
    wrap.locator(f"li[value='{value}']").first.click()
    # Small settle to let the widget commit the selection before we move on
    page.wait_for_timeout(100)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_SC_SESSION_CONFIG_BASE = SessionConfig(
    name="simplecast",
    session_file=_SESSION_FILE,
    is_login_url=url_marker_login_check(_LOGIN_URL_MARKERS),
    target_url="",  # filled in per-call below from env/config/default
    headless_env="SIMPLECAST_HEADLESS",
    login_timeout_env="SIMPLECAST_LOGIN_TIMEOUT",
    chrome_path_env="SIMPLECAST_CHROME_PATH",
    default_timeout_ms=_DEFAULT_TIMEOUT,
    no_login_recovery=is_hosted(),
)


def _resolve_upload_url() -> str:
    """Resolve the new-episode URL from env, config, or the module default."""
    cfg_upload_url = ""
    try:
        from core.config import load_config
        cfg_upload_url = (
            load_config().get("simplecast", {}).get("upload_url", "") or ""
        ).strip()
    except Exception as e:  # noqa: BLE001 — fall back to env/default URL
        logger.debug("simplecast: could not read upload_url from config: %s", e)
    return (
        os.environ.get("SIMPLECAST_UPLOAD_URL", "").strip()
        or cfg_upload_url
        or _DEFAULT_UPLOAD_URL
    )


def upload_episode(entry, elements=None, progress_callback=None) -> dict:
    """Upload a podcast episode using a saved Simplecast session.

    Same signature and return shape as previous versions of this module.

    Progress phases via progress_callback:
        "launching", "awaiting_login" (first-run only), "navigating",
        "filling_form", "uploading_audio", "publishing", "scheduling", "done"
    """
    if elements is not None and not getattr(elements, "sc_enabled", True):
        return {"success": True, "skipped": True, "url": None}

    # ---- Validate inputs ----
    file_path = getattr(entry, "podcast_path", None)
    if not file_path or not os.path.isfile(file_path):
        return {"success": False, "error": f"Podcast file not found: {file_path}"}

    title = (getattr(entry, "podcast_title", "") or "").strip()
    if not title:
        title = (getattr(entry, "youtube_title", "") or "").strip()
    if not title:
        return {"success": False, "error": "No title provided for podcast episode"}

    sc_description = elements is None or getattr(elements, "sc_description", True)
    sc_schedule    = elements is None or getattr(elements, "sc_schedule", True)

    description = (getattr(entry, "description", "") or "") if sc_description else ""
    if sc_description and description:
        footer = _podcast_description_footer()
        if footer:
            description = f"{description}\n\n{footer}"
    schedule_dt = getattr(entry, "podcast_schedule_dt", None) if sc_schedule else None

    upload_url = _resolve_upload_url()
    cfg = SessionConfig(
        name=_SC_SESSION_CONFIG_BASE.name,
        session_file=_SC_SESSION_CONFIG_BASE.session_file,
        is_login_url=_SC_SESSION_CONFIG_BASE.is_login_url,
        target_url=upload_url,
        headless_env=_SC_SESSION_CONFIG_BASE.headless_env,
        login_timeout_env=_SC_SESSION_CONFIG_BASE.login_timeout_env,
        chrome_path_env=_SC_SESSION_CONFIG_BASE.chrome_path_env,
        default_timeout_ms=_SC_SESSION_CONFIG_BASE.default_timeout_ms,
        no_login_recovery=_SC_SESSION_CONFIG_BASE.no_login_recovery,
    )

    # ---- Run ----
    try:
        with PlaywrightSession(cfg, progress_callback=progress_callback) as sess:
            page = sess.page
            assert page is not None

            _emit(progress_callback, "filling_form")
            _fill_title(page, title)
            logger.info("SimpleCast: filled title: %s", title)

            if description:
                try:
                    _fill_description(page, description)
                    logger.info("SimpleCast: filled summary (%d chars)", len(description))
                except Exception as exc:
                    logger.warning("SimpleCast: could not fill summary: %s", exc)
                try:
                    _fill_notes(page, description)
                    logger.info("SimpleCast: filled episode notes")
                except Exception as exc:
                    logger.warning("SimpleCast: could not fill notes: %s", exc)

            _emit(progress_callback, "uploading_audio")
            try:
                _upload_audio(page, file_path)
                logger.info("SimpleCast: audio attached: %s", file_path)
            except Exception as exc:
                return {"success": False, "error": f"Audio upload failed: {exc}"}
            _wait_for_audio_ready(page, _UPLOAD_TIMEOUT)

            _emit(progress_callback, "publishing")
            try:
                _click_save(page)
            except Exception as exc:
                return {"success": False, "error": f"Save failed: {exc}"}

            episode_url = page.url
            m = _EPISODE_ID_RE.search(episode_url)
            episode_id = m.group(1) if m else ""
            logger.info(
                "SimpleCast: episode saved as draft: %s (id=%s)",
                episode_url, episode_id or "?",
            )

            if schedule_dt:
                _emit(progress_callback, "scheduling")
                try:
                    _schedule_episode(page, schedule_dt)
                    logger.info("SimpleCast: scheduled for %s", schedule_dt)
                except Exception as exc:
                    logger.warning(
                        "SimpleCast: scheduling failed, left as draft: %s", exc
                    )
                    _emit(progress_callback, "done")
                    # Episode IS saved as a draft, but the user asked for a
                    # scheduled publish and we didn't deliver it. Surface that
                    # rather than report success — otherwise calendar/history
                    # show the row as if it'll publish on its own.
                    return {
                        "success": False,
                        "partial": True,
                        "needs_manual": True,
                        "url": episode_url,
                        "external_id": episode_id,
                        "error": (
                            f"Episode saved as draft but scheduling failed: {exc}. "
                            "Open the SimpleCast dashboard and schedule it manually."
                        ),
                    }

            _emit(progress_callback, "done")
            return {"success": True, "url": episode_url, "external_id": episode_id}

    except SessionExpiredError:
        # Hosted mode: let the orchestrator surface the actionable re-Connect
        # message instead of swallowing it as a generic RuntimeError below.
        raise
    except PlaywrightTimeout as exc:
        return {"success": False, "error": f"SimpleCast timed out: {exc}"}
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("SimpleCast: unexpected error")
        return {"success": False, "error": str(exc)}


