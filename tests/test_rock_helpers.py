"""Tests for the Rock uploader's pure helper functions.

These don't touch Playwright — they cover the text-normalization and date-
formatting logic we'd otherwise have to verify manually.
"""
from __future__ import annotations

from datetime import date

from uploaders.rock import (
    normalize_vista_content,
    parent_title,
    reflection_title,
    email_title,
    compose_email_message,
    _format_date_for_rock,
)


def test_collapses_whitespace():
    src = "Line one\n  line two\n\tline three"
    assert normalize_vista_content(src, "") == "Line one line two line three"


def test_appends_passage_when_missing():
    out = normalize_vista_content("verse text", "Acts 1:1")
    assert out.endswith("– Acts 1:1")


def test_does_not_double_append_passage():
    src = "verse text – Acts 1:1"
    out = normalize_vista_content(src, "Acts 1:1")
    # No extra " – Acts 1:1" appended.
    assert out == "verse text – Acts 1:1"


def test_recognizes_dash_variants():
    """Hyphen, en-dash, em-dash all count as already-present reference."""
    for dash in ("-", "–", "—"):
        src = f"text {dash} Psalm 23:1"
        assert normalize_vista_content(src, "Psalm 23:1") == src


def test_empty_scripture_returns_empty():
    assert normalize_vista_content("", "Psalm 1:1") == ""


def test_reflection_title_no_zero_pad():
    assert reflection_title(date(2026, 5, 1)) == "May 1"
    assert reflection_title(date(2026, 12, 25)) == "Dec 25"


def test_parent_title():
    assert parent_title(date(2026, 5, 1)) == "Daily Life May 1"


def test_format_date_for_rock_no_zero_pad():
    assert _format_date_for_rock(date(2026, 4, 7)) == "4/7/2026"
    assert _format_date_for_rock(date(2026, 12, 25)) == "12/25/2026"


# --- Daily Life email channel helpers ---------------------------------------

def test_email_title_full_month_day_year():
    # Matches the production convention: "Daily Life May 31, 2026".
    assert email_title(date(2026, 5, 31)) == "Daily Life May 31, 2026"
    assert email_title(date(2026, 1, 1)) == "Daily Life January 1, 2026"


def test_compose_email_prepends_description_above_footer():
    footer = "Here is today's Daily Life:"
    desc = "The way you live may either distort someone's view of God or help them see him more clearly."
    assert compose_email_message(desc, footer) == f"{desc}\n\n{footer}"


def test_compose_email_blank_description_keeps_footer():
    footer = "Here is today's Daily Life:"
    assert compose_email_message("", footer) == footer
    assert compose_email_message("   ", footer) == footer


def test_compose_email_blank_footer_returns_description():
    assert compose_email_message("Just the line.", "") == "Just the line."


def test_compose_email_strips_surrounding_whitespace():
    out = compose_email_message("  desc  ", "  footer  ")
    assert out == "desc\n\nfooter"
