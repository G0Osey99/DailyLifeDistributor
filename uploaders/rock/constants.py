"""URLs, channel GUIDs, selectors, and timeouts for the Rock uploader.

These were previously top-of-file constants in `rock_uploader.py`.
Keeping them in one module lets the rest of the package import only
what it needs and makes "where do I change selector X?" obvious.
"""
from __future__ import annotations

import os
import re

# ---- Base URLs ----
_BASE_URL = os.environ.get("ROCK_BASE_URL", "https://rock.lcbcchurch.com").rstrip("/")
_LOGIN_URL = f"{_BASE_URL}/Login"
_HOME_URL = f"{_BASE_URL}/"

# Rock's login page is page id 3. Match `/page/3` only as the *whole* segment
# (followed by end, slash, or query) so we don't false-positive on pages like
# `/page/343` (the content-channel listing) or `/page/30`.
_LOGIN_PAGE_RE = re.compile(r"/page/3(?:[/?]|$)")


def looks_like_login(url: str) -> bool:
    return "/Login" in url or bool(_LOGIN_PAGE_RE.search(url))


# Project root (two levels up: uploaders/rock/ → uploaders/ → project root).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SESSION_FILE = os.path.join(_PROJECT_ROOT, "rock_session.json")

_LOGIN_TIMEOUT = int(os.environ.get("ROCK_LOGIN_TIMEOUT", "300"))
_HEADLESS = os.environ.get("ROCK_HEADLESS", "").lower() == "true"
_CHROME_PATH = os.environ.get("ROCK_CHROME_PATH")

# Default Save / load timeouts. Rock's saves are usually fast, but image
# uploads can stall behind antivirus on the first byte.
_NAV_TIMEOUT_MS = 30_000
_UPLOAD_TIMEOUT_MS = 5 * 60_000
# Image uploads are small (thumbnails); confirm the BinaryFile id flipped
# within a short window rather than holding a full 5-min Save timeout. On
# timeout the caller logs and proceeds to Save (non-fatal) instead of failing
# the whole item — see RockBrowserClient._upload_image_by_selector.
_IMAGE_UPLOAD_TIMEOUT_MS = 45_000


# ---- Content Channels (captured during recon, 2026-04-27) ----
# Parent uses GUID for listing URLs. Child channels also have integer IDs
# that the "Add Child Item" picker dropdown uses.
_CHANNEL_GUID_PARENT = "5a2ece2c-88b0-47eb-bea0-cbdb50e5c86c"  # Daily Experience

# Child channel GUIDs hardcoded 2026-04-28. These were previously
# discovered at runtime by scraping the /web/content tile grid, but
# that page's render is flaky under headless — one tile occasionally
# fails to land in the DOM within Playwright's timeout, taking the
# whole run down. Hardcoding eliminates that failure mode entirely.
# If Rock ever rotates these (rare for production channels), copy the
# new value out of the channel listing URL: the GUID after
# `?ContentChannelGuid=` on https://rock.lcbcchurch.com/page/343.
_CHANNEL_GUID_REFLECTION = "5a961ea6-2f47-43c4-9190-8d21fe7c5898"
_CHANNEL_GUID_SPOTLIGHT = "3199ba17-c18a-4a0e-82d6-6ff2160c145c"
_CHANNEL_GUID_VISTA = "3c569f1a-d6c0-485f-994e-d993dc5df641"

_CHANNEL_NAME_PARENT = "Daily Experience"
_CHANNEL_NAME_SPOTLIGHT = "Daily Experience Spotlights"
_CHANNEL_NAME_VISTA = "Daily Experience Vistas"
_CHANNEL_NAME_REFLECTION = "Daily Experience Reflections"

# ---- Daily Life *email* content channel (separate from Daily Experience) ----
# This is the email/SMS broadcast channel, not the in-app Daily Experience.
# Captured via scripts/rock_email_recon.py on 2026-05-21. The listing lives
# on the same /page/343 grid; the new-item form is /ContentChannelItem/0
# ?ContentChannelId=24.
_CHANNEL_GUID_EMAIL = "2182c1f3-8f8c-44f3-987f-75a698fe44a7"
_CHANNEL_NAME_EMAIL = "Daily Life"
_CHANNEL_ID_EMAIL = 24

# The email channel's custom attributes are server-rendered with stable
# numeric attribute ids. We select by id *suffix* (id$=) so the long
# `ctl00_main_..._` ASP.NET prefix can change without breaking us, while the
# attribute id pins the specific field. Verified 2026-05-21.
_SEL_EMAIL_MESSAGE = 'textarea[id$="attribute_field_59105"]'
_SEL_EMAIL_SMS = 'textarea[id$="attribute_field_59102"]'
_SEL_EMAIL_YOUTUBE_LINK = 'input[id$="attribute_field_59649"]'
_SEL_EMAIL_SENT = 'select[id$="attribute_field_58362"]'
# Thumbnail image-uploader: the file <input> and the hidden binary-file id
# that flips from "0" to the new BinaryFile id once the upload lands.
_SEL_EMAIL_THUMB_FILE = 'input[id$="attribute_field_58168_fu"]'
_SEL_EMAIL_THUMB_HF = 'input[id*="attribute_field_58168"][id$="hfBinaryFileId"]'
# Start date input (same control id pattern as the parent channel form).
_SEL_EMAIL_START = 'input[id$="dpStart"]'

# Integer channel IDs as seen in the Add-Child-Item modal's dropdown.
# Used when linking existing children to a parent.
_CHANNEL_ID_REFLECTION = 124
_CHANNEL_ID_SPOTLIGHT = 125
_CHANNEL_ID_VISTA = 126
# (123 = Daily Experience Scripture — legacy, ignored)


# ---- URL patterns ----
# Channel listing: /page/343?ContentChannelGuid=<guid>
_CHANNEL_LIST_URL_TMPL = f"{_BASE_URL}/page/343?ContentChannelGuid={{guid}}"
# Item edit: /ContentChannelItem/<id>. New-item form is /ContentChannelItem/0
# so we require a *positive* id when we want to detect "Save succeeded".
_ITEM_URL_RE = re.compile(r"/ContentChannelItem/(\d+)")
_SAVED_ITEM_URL_RE = re.compile(r"/ContentChannelItem/([1-9]\d*)\b")


# ---- Common selectors ----
_SEL_SAVE_LINK = 'a:has-text("Save")'
_SEL_CANCEL_LINK = 'a:has-text("Cancel")'
_SEL_TITLE_INPUT = 'input[placeholder="Enter a title..."]'

# EditorJS structured content
_SEL_SCE_WRAPPER = ".structure-content-editor"
_SEL_SCE_EDITABLE = '.structure-content-editor [contenteditable="true"]'

# Image uploader (file input lives inside the form-group with a matching label)
_SEL_IMAGE_FILE_INPUT_TMPL = (
    '.form-group.image-uploader:has(> label:has-text("{label}")) input[type="file"]'
)
_SEL_IMAGE_PREVIEW_TMPL = (
    '.form-group.image-uploader:has(> label:has-text("{label}")) '
    'a[href*="/GetImage.ashx?id="]'
)
