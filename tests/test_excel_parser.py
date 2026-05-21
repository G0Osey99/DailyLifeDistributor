"""Date-parsing tests for ExcelParser._parse_date.

Tests are pure (no Excel file required) — we instantiate the class with an
empty mapping just so the parser exists.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from core.excel_parser import ExcelParser


@pytest.fixture
def parser():
    return ExcelParser({"sharepoint_docx": "", "excel_mapping": {}})


def test_parses_iso_string(parser):
    assert parser._parse_date("2026-04-29") == date(2026, 4, 29)


def test_parses_us_format(parser):
    assert parser._parse_date("4/29/2026") == date(2026, 4, 29)


def test_parses_long_month(parser):
    assert parser._parse_date("April 29, 2026") == date(2026, 4, 29)


def test_parses_six_digit_yymmdd_fallback(parser):
    """Fallback to digit-stripping interprets 6-digit as YY/MM/DD."""
    assert parser._parse_date("260429") == date(2026, 4, 29)


def test_parses_datetime_object(parser):
    assert parser._parse_date(datetime(2026, 4, 29, 10, 0)) == date(2026, 4, 29)


def test_returns_none_for_empty(parser):
    assert parser._parse_date("") is None
    assert parser._parse_date(None) is None


def test_returns_none_for_garbage(parser):
    assert parser._parse_date("hello") is None


def test_get_metadata_for_date_returns_default_when_missing(parser):
    """An unmapped date must return a fully-populated empty dict so callers
    can `.get(...)` without KeyError."""
    md = parser.get_metadata_for_date("2099-01-01")
    assert md["description"] == ""
    assert md["tags"] == []
    assert "passage" in md
    assert "vista_caption" in md
