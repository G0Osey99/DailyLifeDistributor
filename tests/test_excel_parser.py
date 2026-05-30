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


def test_duplicate_date_rows_collapse_and_warn(tmp_path):
    """CORR-011: two rows for the same date keep the last (last-write-win,
    unchanged) but the parser surfaces a last_error so the operator can spot
    the repeated-date mistake instead of silently losing a day's fields."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append(["Date", "Title"])
    ws.append(["2026-05-21", "First title"])
    ws.append(["2026-05-21", "Second title"])  # duplicate date
    ws.append(["2026-05-22", "Other day"])
    path = tmp_path / "dup.xlsx"
    wb.save(path)

    p = ExcelParser({"sharepoint_docx": str(path), "excel_mapping": {
        "sheet_name": "Plan", "date_column": "Date",
        "youtube_title_column": "Title",
    }})
    result = p.get_metadata()
    # Two distinct dates; the duplicate kept the LAST row's value.
    assert set(result) == {"2026-05-21", "2026-05-22"}
    assert result["2026-05-21"]["youtube_title"] == "Second title"
    # The collapse was surfaced.
    assert "duplicate" in p.last_error.lower()


def test_get_metadata_for_date_returns_default_when_missing(parser):
    """An unmapped date must return a fully-populated empty dict so callers
    can `.get(...)` without KeyError."""
    md = parser.get_metadata_for_date("2099-01-01")
    assert md["description"] == ""
    assert md["tags"] == []
    assert "passage" in md
    assert "vista_caption" in md
