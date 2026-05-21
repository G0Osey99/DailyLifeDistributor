"""Pure text/date helpers for the Rock uploader.

No Playwright import — these are easy to unit-test and the orchestrator's
date formatting / scripture normalization stays in one obvious place.
"""
from __future__ import annotations

import re
from datetime import date

from .constants import _SAVED_ITEM_URL_RE


def _extract_item_id(url: str) -> int:
    m = _SAVED_ITEM_URL_RE.search(url)
    if not m:
        raise RuntimeError(
            f"Expected /ContentChannelItem/<positive id> in URL, got {url!r}. "
            "If id=0, the form is still on the create page and Save never "
            "redirected — likely a validation error. Check the browser."
        )
    return int(m.group(1))


def _format_date_for_rock(d: date) -> str:
    """Rock's date input accepts m/d/YYYY without leading zeros."""
    return f"{d.month}/{d.day}/{d.year}"


def normalize_vista_content(scripture: str, passage: str) -> str:
    """Clean up Excel-formatted scripture for the Vista Content field.

    The publication spreadsheet stores scripture cells with hard line
    breaks and indentation that mirror the printed verse layout. When
    typed into the Rock editor those breaks survive and the saved Vista
    renders with awkward gaps and indents.

    This helper:

    * Collapses every run of whitespace (newlines, tabs, multi-spaces)
      to a single space and trims the ends, so the scripture reads as
      flowing prose.
    * Appends `– {passage}` when the passage reference isn't already
      present at the end. Sheet authors are inconsistent — some rows
      include the reference with an en-dash, some include it on a new
      line with no dash, some omit it entirely. We only append when
      the reference is genuinely missing from the tail; we do not
      reformat references that are present (e.g. we don't insert a
      missing en-dash) so the source text stays the source of truth
      everywhere it already gives us a reference.

    Dash variants (en-dash U+2013, em-dash U+2014, hyphen) are treated
    as equivalent when checking whether the reference is already in
    place, so "Psalm 145:8–9" matches "Psalm 145:8-9".
    """
    if not scripture:
        return scripture

    text = re.sub(r"\s+", " ", scripture).strip()

    if not passage:
        return text

    def _norm(s: str) -> str:
        return re.sub(r"[–—]", "-", s).lower().strip()

    passage_norm = _norm(passage)
    # Look at a tail window slightly larger than the passage itself so a
    # leading "– " or "- " doesn't push the reference out of the slice.
    tail_window = max(len(passage) + 5, 30)
    if _norm(text[-tail_window:]).endswith(passage_norm):
        return text

    return f"{text} – {passage}"


def reflection_title(d: date) -> str:
    """3-letter month + day, no zero-pad. E.g. 2026-05-10 -> 'May 10'."""
    return f"{d.strftime('%b')} {d.day}"


def parent_title(d: date) -> str:
    """Full month + day, no zero-pad. E.g. 2026-05-10 -> 'Daily Life May 10'."""
    return f"Daily Life {d.strftime('%B')} {d.day}"


def email_title(d: date) -> str:
    """Email content-channel item title.

    Matches the production convention seen on the Daily Life email channel:
    full month, day, comma, four-digit year. E.g. 2026-05-31 ->
    'Daily Life May 31, 2026'.
    """
    return f"Daily Life {d.strftime('%B')} {d.day}, {d.year}"


def compose_email_message(description: str, existing_body: str) -> str:
    """Build the Email/SMS Message body: the day's description above the
    channel's standing footer.

    The Daily Life email channel pre-fills both the Email Message and SMS
    Message attributes with a standing footer (production value: "Here is
    today's Daily Life:"). The per-day devotional description goes *above*
    that footer, separated by a blank line — see the May 31 reference item.

    We prepend onto whatever the channel pre-filled (`existing_body`)
    rather than hard-coding the footer string, so a future edit to the
    channel's default footer flows through automatically. If the channel
    ever ships a blank default we still emit just the description.

    Idempotent-ish: if `description` is empty we return the existing body
    untouched; we never stack a description on top of itself because the
    caller always works from a freshly opened (footer-only) Add form.
    """
    description = (description or "").strip()
    footer = (existing_body or "").strip()
    if not description:
        return footer
    if not footer:
        return description
    return f"{description}\n\n{footer}"
