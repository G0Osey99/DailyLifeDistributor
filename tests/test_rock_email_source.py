"""Unit tests for core.refresh.rock_email_source row parsing.

The email channel grid uses a different column layout than Daily Experience
(Title@0, Date@2). Headers captured live 2026-05-22 put the date ("Start")
at index 5, so the source must locate columns by header name.
"""
from datetime import date

from core.calendar_refresh import ExternalItem
from core.refresh import rock_email_source as r

# Real header row from the live email channel grid.
_HEADERS = ["Title", "Thumbnail", "YouTube Link", "Sent",
            "Youtube Daily Life Media Sync", "Start", "", "Expire",
            "Priority", "Created By", "Tags", "", ""]


def _row(item_id, title, start):
    cells = ["", "", "", "", "", "", "", "", "0", "Ryker", "", "", ""]
    cells[0] = title
    cells[5] = start
    return {"id": item_id, "cells": cells}


def test_col_index_finds_start_column():
    assert r._col_index(_HEADERS, r._DATE_HEADERS) == 5
    assert r._col_index(_HEADERS, r._TITLE_HEADERS) == 0
    # "Date" header is accepted as a fallback for the date column.
    assert r._col_index(["Title", "Date"], r._DATE_HEADERS) == 1
    assert r._col_index(["Title", "Foo"], r._DATE_HEADERS) is None


def test_rows_to_items_status_is_date_based():
    today = date(2026, 5, 22)
    rows = [
        _row("18012", "Daily Life May 31, 2026", "5/31/2026"),  # future -> scheduled
        _row("18000", "Daily Life May 10, 2026", "5/10/2026"),  # past -> published
        _row("17000", "Daily Life Jan 1, 2026", "1/1/2026"),    # outside window
    ]
    items = r._rows_to_items(_HEADERS, rows,
                             date(2026, 4, 22), date(2026, 11, 18), today, guid="g")
    by_id = {it.external_id: it for it in items}
    assert set(by_id) == {"18012", "18000"}  # out-of-window dropped

    assert isinstance(by_id["18012"], ExternalItem)
    assert by_id["18012"].platform == "rock_email"
    assert by_id["18012"].status == "scheduled"
    assert by_id["18012"].iso_date == "2026-05-31"
    assert by_id["18012"].title == "Daily Life May 31, 2026"

    assert by_id["18000"].status == "published"
    assert by_id["18000"].iso_date == "2026-05-10"


def test_rows_to_items_no_date_column():
    rows = [_row("1", "t", "5/10/2026")]
    assert r._rows_to_items(["Title", "Foo"], rows,
                            date(2026, 1, 1), date(2026, 12, 31), date(2026, 5, 22)) == []
