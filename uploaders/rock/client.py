"""RockBrowserClient — Playwright lifecycle + page actions for Rock.

The orchestrator is unaware of Playwright; it talks to this client (or
any RockClient) and gets ItemRefs back. Auth uses the shared
core.playwright_session.PlaywrightSession so login/launch behaviour
stays consistent with the SimpleCast and Vista uploaders.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional

try:
    from playwright.sync_api import (
        Page,
        TimeoutError as PlaywrightTimeout,
        sync_playwright,
    )
except ImportError:  # pragma: no cover
    sync_playwright = None
    PlaywrightTimeout = Exception
    Page = object  # type: ignore[assignment,misc]

from core.hosted import is_hosted
from core.playwright_session import PlaywrightSession, SessionConfig

from .constants import (
    _CHANNEL_GUID_EMAIL,
    _CHANNEL_GUID_PARENT,
    _CHANNEL_GUID_REFLECTION,
    _CHANNEL_GUID_SPOTLIGHT,
    _CHANNEL_GUID_VISTA,
    _CHANNEL_ID_REFLECTION,
    _CHANNEL_ID_SPOTLIGHT,
    _CHANNEL_ID_VISTA,
    _CHANNEL_LIST_URL_TMPL,
    _CHANNEL_NAME_EMAIL,
    _CHANNEL_NAME_PARENT,
    _CHANNEL_NAME_REFLECTION,
    _CHANNEL_NAME_SPOTLIGHT,
    _CHANNEL_NAME_VISTA,
    _HOME_URL,
    _IMAGE_UPLOAD_TIMEOUT_MS,
    _ITEM_URL_RE,
    _NAV_TIMEOUT_MS,
    _PROJECT_ROOT,
    _SAVED_ITEM_URL_RE,
    _SEL_EMAIL_MESSAGE,
    _SEL_EMAIL_SMS,
    _SEL_EMAIL_START,
    _SEL_EMAIL_THUMB_FILE,
    _SEL_EMAIL_THUMB_HF,
    _SEL_EMAIL_YOUTUBE_LINK,
    _SEL_IMAGE_FILE_INPUT_TMPL,
    _SEL_TITLE_INPUT,
    _SESSION_FILE,
    _UPLOAD_TIMEOUT_MS,
    looks_like_login,
)
from .fields import (
    EmailFields,
    ItemRef,
    ParentFields,
    ReflectionFields,
    SpotlightFields,
    VistaFields,
)
from .text import compose_email_message
from .text import _extract_item_id, _format_date_for_rock


log = logging.getLogger(__name__)


_ROCK_SESSION_CONFIG = SessionConfig(
    name="rock",
    session_file=_SESSION_FILE,
    is_login_url=looks_like_login,
    target_url=_HOME_URL,
    headless_env="ROCK_HEADLESS",
    login_timeout_env="ROCK_LOGIN_TIMEOUT",
    chrome_path_env="ROCK_CHROME_PATH",
    default_timeout_ms=_NAV_TIMEOUT_MS,
    no_login_recovery=is_hosted(),
)


class RockBrowserClient:
    """Drives Rock through Playwright. Use as a context manager.

    >>> with RockBrowserClient() as rock:
    ...     parent = rock.create_parent(parent_fields)
    ...     spot = rock.create_spotlight(spotlight_fields)
    ...     rock.link_spotlight_to_parent(parent, spot)
    """

    def __init__(self) -> None:
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright is not installed. Run `pip install playwright`."
            )
        self._session: Optional[PlaywrightSession] = None
        self._page: Optional[Page] = None
        self._channel_guids: dict[str, str] = {
            _CHANNEL_NAME_PARENT: _CHANNEL_GUID_PARENT,
            _CHANNEL_NAME_REFLECTION: _CHANNEL_GUID_REFLECTION,
            _CHANNEL_NAME_SPOTLIGHT: _CHANNEL_GUID_SPOTLIGHT,
            _CHANNEL_NAME_VISTA: _CHANNEL_GUID_VISTA,
        }

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> "RockBrowserClient":
        self._session = PlaywrightSession(_ROCK_SESSION_CONFIG)
        self._session.__enter__()
        self._page = self._session.page
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc is not None and self._page is not None:
                # Capture state on failure to make tomorrow's debugging
                # possible without a re-run.
                shot = os.path.join(
                    _PROJECT_ROOT,
                    f"rock_failure_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
                )
                try:
                    self._page.screenshot(path=shot, full_page=True)
                    log.error("Saved failure screenshot to %s", shot)
                except Exception as screenshot_exc:  # noqa: BLE001
                    log.error("Failed to save screenshot: %s", screenshot_exc)
        finally:
            if self._session is not None:
                self._session.__exit__(exc_type, exc, tb)
            self._session = None
            self._page = None

    def screenshot(self, path: str) -> None:
        """Public helper for smoke scripts to capture mid-run state."""
        assert self._page is not None
        self._page.screenshot(path=path, full_page=True)

    def _goto(self, url: str) -> None:
        """Navigate using `domcontentloaded` as the readiness gate.

        Rock's pages keep the network busy long after the DOM is parsed —
        Chosen widgets, profile chrome, content thumbnails, and async
        grid refreshes all keep firing requests. Default `wait_until="load"`
        waits for `window.onload`, which on heavy admin pages can fail to
        fire within Playwright's 30s timeout even though the page is
        fully usable. Every navigation in this client follows up with an
        explicit wait on a selector or URL we actually need.
        """
        page = self._page
        assert page is not None
        page.goto(url, wait_until="domcontentloaded")

    # -- idempotency ------------------------------------------------------

    def find_existing_parent_for_date(self, publish_date: date) -> Optional[ItemRef]:
        """Search the parent channel's listing for an item with this Active
        date. Returns the first match or None.

        We match on the publish date column (mm/dd/yyyy) rather than title,
        because two parents could share a title across years.
        """
        page = self._page
        assert page is not None
        url = _CHANNEL_LIST_URL_TMPL.format(guid=_CHANNEL_GUID_PARENT)
        self._goto(url)
        # Listing renders mm/dd/yyyy without leading zeros (e.g. "4/12/2026").
        target = f"{publish_date.month}/{publish_date.day}/{publish_date.year}"
        rows = page.locator(f'tr:has(td:text-is("{target}"))')
        if rows.count() == 0:
            return None
        first_row = rows.first
        title_cell = first_row.locator("td").nth(0)
        title_cell.click()
        page.wait_for_url(_ITEM_URL_RE, wait_until="domcontentloaded")
        return ItemRef(id=_extract_item_id(page.url))

    # -- create -----------------------------------------------------------

    def create_parent(self, fields: ParentFields) -> ItemRef:
        page = self._open_add_item_form(_CHANNEL_NAME_PARENT, _CHANNEL_GUID_PARENT)
        self._fill_title(fields.title)

        # Active date — plain text input wrapped in a Bootstrap datepicker.
        # We set the value via JS rather than Playwright fill() for two reasons:
        #   1. Escape reverts the value to today (datepicker cancel-edit).
        #   2. fill() opens the calendar overlay, which lingers and covers
        #      later fields like the Enable Prayer checkbox — clicks then
        #      land on the overlay instead of the target.
        page.evaluate(
            """({id, value}) => {
                const el = document.getElementById(id);
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            {
                "id": "ctl00_main_ctl14_ctl01_ctl00_dpStart",
                "value": _format_date_for_rock(fields.active_date),
            },
        )

        # Enable Prayer is a checkbox wrapped by a <label>. Playwright's
        # check/uncheck (even with force=True) double-toggles in this
        # layout — the synthetic click bubbles to the label, which fires
        # another click on the input — so net state doesn't change.
        # A JS .click() toggles cleanly. Read current state and only click
        # if we need to change it.
        prayer_box = page.get_by_label("Enable Prayer")
        cb_id = prayer_box.get_attribute("id")
        page.evaluate(
            """({id, target}) => {
                const el = document.getElementById(id);
                if (el.checked !== target) el.click();
            }""",
            {"id": cb_id, "target": fields.enable_prayer},
        )

        # Prayer Count is an Ion Range Slider. The visible input is readonly
        # (class "irs-hidden-input"); fill() fails on it. Drive the slider
        # through its jQuery plugin instead.
        prayer_input = page.get_by_label("Prayer Count")
        input_id = prayer_input.get_attribute("id")
        page.evaluate(
            """({id, value}) => {
                const $el = window.jQuery('#' + id);
                const irs = $el.data('ionRangeSlider');
                if (irs) {
                    irs.update({from: value});
                } else {
                    $el.val(value);
                }
                $el.trigger('change');
            }""",
            {"id": input_id, "value": fields.prayer_count},
        )

        return self._save_and_capture_id(
            fallback_title=fields.title,
            fallback_channel_guid=_CHANNEL_GUID_PARENT,
        )

    def create_spotlight(self, fields: SpotlightFields) -> ItemRef:
        page = self._open_add_item_form(
            _CHANNEL_NAME_SPOTLIGHT,
            self._channel_guids[_CHANNEL_NAME_SPOTLIGHT],
        )
        self._fill_title(fields.title)
        page.get_by_label("Tagline").fill(fields.tagline)
        # The radio's <input> is hidden behind a styled .label-text span
        # that intercepts pointer events; force=True still toggles the
        # underlying input.
        page.get_by_role("radio", name=fields.video_orientation).check(force=True)

        # All three Media dropdowns are real <select> elements (Chosen.js
        # hides them visually but the underlying option list is server-
        # rendered). select_option works on them directly even when
        # display:none. The onchange handlers fire ASP.NET __doPostBack
        # which re-renders the next select; we wait for the next select's
        # option list to repopulate before touching it.
        self._select_cascading("ddlMediaAccount", label=fields.media_account)
        self._wait_for_cascade("ddlMediaFolder", expected_label=fields.media_folder)
        self._select_cascading("ddlMediaFolder", label=fields.media_folder)
        self._wait_for_cascade("ddlMediaElement", expected_label=fields.media_reference)
        self._select_cascading("ddlMediaElement", label=fields.media_reference)

        return self._save_and_capture_id(
            fallback_title=fields.title,
            fallback_channel_guid=self._channel_guids[_CHANNEL_NAME_SPOTLIGHT],
        )

    def create_vista(self, fields: VistaFields) -> ItemRef:
        page = self._open_add_item_form(
            _CHANNEL_NAME_VISTA,
            self._channel_guids[_CHANNEL_NAME_VISTA],
        )
        self._fill_title(fields.title)
        self._fill_structured_content(fields.content)
        page.get_by_label("Tagline").fill(fields.tagline)

        if fields.background_image_path is not None:
            self._upload_image("Background Image", fields.background_image_path)

        # Has Likes Enabled is two anchors styled as a toggle.
        toggle_label = "True" if fields.has_likes_enabled else "False"
        page.locator(
            '.form-group.toggle:has(> label:has-text("Has Likes Enabled")) '
            f'a:text-is("{toggle_label}")'
        ).click()

        # Share Image is intentionally NOT set — Rock auto-derives it from
        # Background Image.
        return self._save_and_capture_id(
            fallback_title=fields.title,
            fallback_channel_guid=self._channel_guids[_CHANNEL_NAME_VISTA],
        )

    def create_reflection(self, fields: ReflectionFields) -> ItemRef:
        page = self._open_add_item_form(
            _CHANNEL_NAME_REFLECTION,
            self._channel_guids[_CHANNEL_NAME_REFLECTION],
        )
        self._fill_title(fields.title)
        self._fill_structured_content(fields.content)
        page.get_by_label("Public Title", exact=True).fill(fields.public_title)
        page.get_by_label("Prompt Title", exact=True).fill(fields.prompt_title)
        # exact=True so "Prompt" doesn't match "Prompt Title" too.
        page.get_by_label("Prompt", exact=True).fill(fields.prompt)
        return self._save_and_capture_id(
            fallback_title=fields.title,
            fallback_channel_guid=self._channel_guids[_CHANNEL_NAME_REFLECTION],
        )

    # -- Daily Life email channel ----------------------------------------

    def find_existing_email_for_date(self, fields: EmailFields) -> Optional[ItemRef]:
        """Return the existing email item for this date if one is already on
        the channel listing, matched by its (date-unique) title. Idempotency
        guard so a re-run never double-schedules the same day's email.
        """
        page = self._page
        assert page is not None
        self._goto(_CHANNEL_LIST_URL_TMPL.format(guid=_CHANNEL_GUID_EMAIL))
        rows = page.locator(f'tr:has(td:text-is("{fields.title}"))')
        if rows.count() == 0:
            return None
        rows.first.locator("td").nth(0).click()
        page.wait_for_url(_SAVED_ITEM_URL_RE, wait_until="domcontentloaded")
        return ItemRef(id=_extract_item_id(page.url))

    def create_email_item(self, fields: EmailFields) -> ItemRef:
        """Create one Daily Life email content-channel item as a draft.

        Leaves Sent="No" (the form default) so the item is queued, not sent.
        Fills Title, Start (send date), Email Message + SMS Message (the
        day's description above the channel's standing footer), the YouTube
        Link (horizontal watch URL), and — when provided — the Thumbnail.
        """
        page = self._open_add_item_form(_CHANNEL_NAME_EMAIL, _CHANNEL_GUID_EMAIL)
        self._fill_title(fields.title)

        # Start date — same readonly Bootstrap datepicker as the parent form.
        # Set via JS (not fill()) so the calendar overlay never opens and
        # Escape can't revert it to today. Located by id suffix.
        page.evaluate(
            """({sel, value}) => {
                const el = document.querySelector(sel);
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            {"sel": _SEL_EMAIL_START, "value": _format_date_for_rock(fields.start_date)},
        )

        # Email Message: prepend the day's description above the pre-filled
        # standing footer. Plain <textarea>, so read-then-fill is reliable.
        email_box = page.locator(_SEL_EMAIL_MESSAGE)
        composed = compose_email_message(fields.description, email_box.input_value())
        email_box.fill(composed)
        if fields.mirror_to_sms:
            sms_box = page.locator(_SEL_EMAIL_SMS)
            sms_composed = compose_email_message(fields.description, sms_box.input_value())
            sms_box.fill(sms_composed)

        if fields.youtube_watch_url:
            page.locator(_SEL_EMAIL_YOUTUBE_LINK).fill(fields.youtube_watch_url)

        if fields.thumbnail_path is not None:
            self._upload_image_by_selector(
                _SEL_EMAIL_THUMB_FILE, _SEL_EMAIL_THUMB_HF, fields.thumbnail_path
            )

        # Sent is left at the form default ("No") — the item is a queued draft.
        return self._save_and_capture_id(
            fallback_title=fields.title,
            fallback_channel_guid=_CHANNEL_GUID_EMAIL,
        )

    def _upload_image_by_selector(self, file_sel: str, hf_sel: str, path: Path) -> None:
        """Upload into an image-uploader addressed by explicit selectors.

        Mirrors `_upload_image` but for widgets we target by id suffix.
        Waits until the widget's hidden binary-file id flips from "0" to the
        new BinaryFile id — the value Save actually posts — so we don't click
        Save before the bytes have landed.

        Readiness detection: instead of the rigid ``hf_sel`` (an exact-id CSS
        match that broke the Rock-Email thumbnail — it polled the full 5-min
        timeout and failed the whole email), walk up from the file input to
        the uploader container and look for the hidden BinaryFile field by
        ``name`` OR ``id`` — the same resilient approach `_upload_image` uses.
        Non-fatal: if readiness can't be confirmed in time we LOG and proceed
        to Save anyway (the upload has very likely finished; Rock posts
        whatever the hidden field holds — so a slow/undetected upload yields
        an email without the thumbnail rather than a 5-minute hard failure).
        """
        page = self._page
        assert page is not None
        page.locator(file_sel).set_input_files(str(path))
        try:
            page.wait_for_function(
                """([fileSel, hfSel]) => {
                    // Primary: the explicit hidden-field selector, if it matches.
                    const direct = document.querySelector(hfSel);
                    if (direct && direct.value && direct.value !== '0') return true;
                    // Resilient: from the file input, walk up to the uploader
                    // container and find the BinaryFile hidden input by name OR id.
                    const fu = document.querySelector(fileSel);
                    let c = fu ? fu.parentElement : null;
                    for (let i = 0; i < 5 && c; i++) {
                        const hf = c.querySelector(
                            'input[name*="hfBinaryFileId"],input[id*="hfBinaryFileId"]');
                        if (hf && hf.value && hf.value !== '0') return true;
                        c = c.parentElement;
                    }
                    return false;
                }""",
                arg=[file_sel, hf_sel],
                timeout=_IMAGE_UPLOAD_TIMEOUT_MS,
            )
        except PlaywrightTimeout:
            log.warning(
                "Rock image upload (%s) not confirmed within %ss; proceeding to "
                "Save anyway — the email/item is created but may lack the image. "
                "If the thumbnail is consistently missing, re-recon the widget "
                "selectors (scripts/rock_email_recon.py).",
                file_sel, _IMAGE_UPLOAD_TIMEOUT_MS // 1000,
            )

    # -- link -------------------------------------------------------------
    #
    # Rock's "Add Existing Item" picker requires the source channel up front
    # (it drives the Item dropdown's options via postback), so we expose one
    # helper per child type rather than a generic link_child_to_parent.

    def link_spotlight_to_parent(self, parent: ItemRef, spotlight: ItemRef) -> None:
        self._link_existing(parent, spotlight, _CHANNEL_ID_SPOTLIGHT)

    def link_vista_to_parent(self, parent: ItemRef, vista: ItemRef) -> None:
        self._link_existing(parent, vista, _CHANNEL_ID_VISTA)

    def link_reflection_to_parent(self, parent: ItemRef, reflection: ItemRef) -> None:
        self._link_existing(parent, reflection, _CHANNEL_ID_REFLECTION)

    def _link_existing(self, parent: ItemRef, child: ItemRef, channel_id: int) -> None:
        page = self._page
        assert page is not None
        self._goto(parent.edit_url)
        page.locator(
            'a[href*="gChildItems$actionFooterRow$footerGridActions$lbAdd"]'
        ).first.click()
        page.wait_for_selector(
            "#ctl00_main_ctl14_ctl01_ctl00_dlgAddChild_modal_dialog_panel"
        )
        page.locator(
            'select[id*="dlgAddChild_ddlAddExistingItemChannel"]'
        ).select_option(value=str(channel_id))
        # Use ends-with: [id*="...ddlAddExistingItem"] would also match
        # ddlAddExistingItemChannel (substring), causing strict-mode collision.
        page.wait_for_function(
            "() => {"
            "  const s = document.querySelector('select[id$=\"dlgAddChild_ddlAddExistingItem\"]');"
            "  return s && s.options.length > 1;"
            "}"
        )
        # Chosen.js hides the underlying <select> with display:none, so
        # Playwright's actionability check on select_option times out.
        # Set the value via JS and fire jQuery's change event so Chosen
        # and the postback both see the choice.
        page.evaluate(
            """(value) => {
                const $el = window.jQuery('select[id$="dlgAddChild_ddlAddExistingItem"]');
                $el.val(value).trigger('change');
            }""",
            str(child.id),
        )
        page.locator('a[id$="dlgAddChild_lbAddExistingChildItem"]').click()
        page.wait_for_selector(
            "#ctl00_main_ctl14_ctl01_ctl00_dlgAddChild_modal_dialog_panel",
            state="hidden",
        )
        log.info(
            "Linked child item %d (channel %d) -> parent %d",
            child.id, channel_id, parent.id,
        )

    # -- helpers ----------------------------------------------------------

    def _open_add_item_form(self, channel_name: str, channel_guid: str) -> Page:
        page = self._page
        assert page is not None
        self._goto(_CHANNEL_LIST_URL_TMPL.format(guid=channel_guid))
        # Rock renders the Add button in BOTH action rows (header + footer)
        # with identical id/href — use .first to pick one.
        page.locator(
            'a[href*="gContentChannelItems$actionFooterRow$footerGridActions$lbAdd"]'
        ).first.click()
        page.wait_for_selector(_SEL_TITLE_INPUT)
        log.debug("Opened add-item form for channel %s", channel_name)
        return page

    def _fill_title(self, value: str) -> None:
        # exact=True so we don't match "Public Title" / "Prompt Title".
        assert self._page is not None
        self._page.get_by_label("Title", exact=True).fill(value)

    def _fill_structured_content(self, text: str) -> None:
        """Write into Rock's EditorJS-backed Content field.

        Verified against the Reflection Add Item form via Playwright MCP:
        the form-group itself carries class `structure-content-editor` and
        contains a single `.ce-paragraph[contenteditable="true"]` block.

        We use `press_sequentially` (per-character keydown/input) rather
        than `keyboard.insert_text`. EditorJS's onChange doesn't pick up
        the single composition event that `insert_text` emits, so the
        hidden input never gets the JSON and the saved item is blank.

        EditorJS's onChange is *debounced*. After typing we blur to queue
        the sync, then BLOCK until the hidden `sceContent_hfValue` input
        has actually changed from its pre-type baseline. Without that
        wait, forms whose post-content steps are fast (Reflection runs
        three quick .fill() calls before Save) race Save against the
        debounced sync and the saved item ends up blank.
        """
        page = self._page
        assert page is not None
        initial_hf_value = page.evaluate(
            """() => {
                const inp = document.querySelector(
                    'input[id*="sceContent_hfValue"]'
                );
                return inp ? (inp.value || '') : '';
            }"""
        )
        editable = page.locator(
            '.form-group.structure-content-editor [contenteditable="true"]'
        ).first
        editable.wait_for(state="visible", timeout=10000)
        editable.click()
        editable.press_sequentially(text, delay=2)
        # Blur to force EditorJS's onChange to sync the hidden input that
        # the postback will read.
        page.locator(_SEL_TITLE_INPUT).first.focus()
        page.wait_for_function(
            """({initial}) => {
                const inp = document.querySelector(
                    'input[id*="sceContent_hfValue"]'
                );
                if (!inp) return false;
                const v = inp.value || '';
                return v !== initial && v.length > 20;
            }""",
            arg={"initial": initial_hf_value},
            timeout=15_000,
        )

    def _upload_image(self, label_text: str, path: Path) -> None:
        """Upload an image into a Rock image-uploader widget by label.

        After `set_input_files` Rock POSTs the bytes to its file handler.
        Each widget has a hidden `hfBinaryFileId` input that starts at "0"
        and gets the new BinaryFile id on success — that's the value the
        form actually posts on Save, so it's the canonical readiness signal.
        """
        page = self._page
        assert page is not None
        sel = _SEL_IMAGE_FILE_INPUT_TMPL.format(label=label_text)
        page.locator(sel).set_input_files(str(path))
        page.wait_for_function(
            """(label) => {
                const groups = document.querySelectorAll('.form-group.image-uploader');
                for (const g of groups) {
                    const lbl = g.querySelector(':scope > label');
                    if (!lbl) continue;
                    const labelWords = (lbl.textContent || '').trim().split(/\\s+/);
                    const want = label.split(/\\s+/);
                    if (labelWords.slice(0, want.length).join(' ') !== label) continue;
                    const hf = g.querySelector('input[name*="hfBinaryFileId"]');
                    if (hf && hf.value && hf.value !== '0') return true;
                }
                return false;
            }""",
            arg=label_text,
            timeout=_UPLOAD_TIMEOUT_MS,
        )

    def _select_cascading(self, suffix: str, *, label: str) -> None:
        """Set a value on a cascading <select> by suffix of its id, by
        visible label. Triggers the onchange postback so the next select
        repopulates.
        """
        if not label:
            return
        page = self._page
        assert page is not None
        page.locator(f'select[id*="{suffix}"]').select_option(label=label, force=True)

    def _wait_for_cascade(self, suffix: str, *, expected_label: str) -> None:
        """Wait for the next-tier <select> to repopulate after a postback."""
        if not expected_label:
            return
        page = self._page
        assert page is not None
        page.wait_for_function(
            """(args) => {
                const sel = document.querySelector(`select[id*="${args.suffix}"]`);
                if (!sel) return false;
                if (sel.options.length <= 1) return false;
                if (!args.expected) return true;
                return Array.from(sel.options).some(o => o.text === args.expected);
            }""",
            arg={"suffix": suffix, "expected": expected_label},
        )

    def _save_and_capture_id(self, *, fallback_title: str, fallback_channel_guid: str) -> ItemRef:
        """Click Save and resolve the new item's id.

        Rock's behaviour after Save varies by channel: sometimes it
        redirects to /ContentChannelItem/<newId>, sometimes back to the
        channel listing. We wait until the URL changes away from /0, then
        either parse the id from the URL or fall back to the listing
        search by the just-saved title.
        """
        page = self._page
        assert page is not None
        # Wait for any in-flight ASP.NET async postback to finish before
        # clicking Save. Some Rock fields (e.g. the parent's Prayer Count
        # attribute) trigger a partial UpdatePanel refresh on change; if
        # Save fires while that's still pending, the click is dropped and
        # the page never navigates.
        page.wait_for_load_state("networkidle")
        page.wait_for_function(
            "() => {"
            "  if (!window.Sys || !Sys.WebForms || !Sys.WebForms.PageRequestManager) return true;"
            "  return !Sys.WebForms.PageRequestManager.getInstance().get_isInAsyncPostBack();"
            "}",
            timeout=_NAV_TIMEOUT_MS,
        )
        before_url = page.url
        # Trigger Save by navigating to the anchor's `javascript:` href
        # rather than clicking. Both Playwright synthetic clicks and
        # in-page `.click()` failed silently under `channel='chrome'`,
        # while `window.location = a.href` works reliably. The href runs
        # `WebForm_DoPostBackWithOptions(...)` in the page's non-strict
        # context, which is required because `__doPostBack` introspects
        # `arguments.caller` and throws under strict mode.
        save_id = page.locator('a[id$="lbSave"]').first.get_attribute("id")
        page.evaluate(
            """(id) => {
                const a = document.getElementById(id);
                window.location.href = a.href;
            }""",
            save_id,
        )
        page.wait_for_function(
            "(prev) => location.href !== prev && !/\\/ContentChannelItem\\/0(\\b|\\?)/.test(location.href)",
            arg=before_url,
            timeout=_NAV_TIMEOUT_MS,
        )
        # Case 1: we landed on the saved item's edit page.
        m = _SAVED_ITEM_URL_RE.search(page.url)
        if m:
            item_id = int(m.group(1))
            log.info("Saved item %d at %s", item_id, page.url)
            return ItemRef(id=item_id)

        # Case 2: we landed on the channel listing. Find the item by title.
        log.info("Save redirected to %s; resolving id by title %r", page.url, fallback_title)
        if "ContentChannelGuid" not in page.url:
            self._goto(_CHANNEL_LIST_URL_TMPL.format(guid=fallback_channel_guid))
        item_id = self._find_item_id_by_title(fallback_title)
        log.info("Resolved newly saved item %d by title", item_id)
        return ItemRef(id=item_id)

    def _find_item_id_by_title(self, title: str) -> int:
        """Search the current channel listing for a row whose title cell
        equals `title` and return that item's id by clicking through to its
        edit page.
        """
        page = self._page
        assert page is not None
        rows = page.locator(f'tr:has(td:text-is("{title}"))')
        count = rows.count()
        if count == 0:
            raise RuntimeError(
                f"Could not find newly saved item with title {title!r} on listing"
            )
        rows.first.locator("td").nth(0).click()
        page.wait_for_url(_SAVED_ITEM_URL_RE, wait_until="domcontentloaded")
        return _extract_item_id(page.url)
