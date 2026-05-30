"""Unit tests for the SimpleCast scheduling math helpers.

The helpers under test (`_compute_schedule_targets`, `_parse_picker_header`,
`_compute_month_delta`) drive a v-calendar date picker in the SimpleCast
dashboard. They were the most fragile uncovered code in the repo: a single
off-by-one in the month delta or a misformatted hour value silently
schedules an episode for the wrong day or hour, with no easy way to spot it
short of catching it on the SimpleCast UI.
"""
from datetime import datetime, timezone

import pytest

from uploaders.simplecast_uploader import (
    _compute_month_delta,
    _compute_schedule_targets,
    _parse_picker_header,
)


# --------- _compute_schedule_targets: naive datetime ---------

def test_targets_naive_datetime_passthrough():
    """A naive datetime should be used as-is (no tz conversion)."""
    s = _compute_schedule_targets(datetime(2026, 5, 13, 14, 30))
    assert s["target"] == datetime(2026, 5, 13, 14, 30)
    assert s["day_id"] == "id-2026-05-13"
    assert s["header"] == "May 2026"
    assert s["aria"] == "Wednesday, May 13, 2026"


def test_targets_hour_mod_12():
    """Hour <li value=> uses 00..11; noon and midnight both map to '00'."""
    assert _compute_schedule_targets(datetime(2026, 5, 13, 0, 0))["hour_value"] == "00"
    assert _compute_schedule_targets(datetime(2026, 5, 13, 12, 0))["hour_value"] == "00"
    assert _compute_schedule_targets(datetime(2026, 5, 13, 1, 0))["hour_value"] == "01"
    assert _compute_schedule_targets(datetime(2026, 5, 13, 11, 0))["hour_value"] == "11"
    assert _compute_schedule_targets(datetime(2026, 5, 13, 13, 0))["hour_value"] == "01"
    assert _compute_schedule_targets(datetime(2026, 5, 13, 23, 0))["hour_value"] == "11"


def test_targets_ampm_boundary():
    """AM/PM boundary at 12:00 — noon is PM, midnight is AM.

    Uses on-grid :55 for the "just before" cases: post-CORR-002 the helper
    rounds to the nearest 5 min, so 11:59 would round up to 12:00 (pm) and
    23:59 to 00:00 next day (am). :55 stays in the same hour.
    """
    assert _compute_schedule_targets(datetime(2026, 5, 13, 0, 0))["ampm_value"] == "am"
    assert _compute_schedule_targets(datetime(2026, 5, 13, 11, 55))["ampm_value"] == "am"
    assert _compute_schedule_targets(datetime(2026, 5, 13, 12, 0))["ampm_value"] == "pm"
    assert _compute_schedule_targets(datetime(2026, 5, 13, 23, 55))["ampm_value"] == "pm"


@pytest.mark.parametrize("minute,expected", [
    (0, "00"),
    (1, "00"),    # rounds down
    (2, "00"),    # rounds down (banker's: 2.5 -> 2 -> 0; here 2/5=0.4 -> 0)
    (3, "05"),    # rounds up
    (7, "05"),
    (8, "10"),
    (29, "30"),
    (30, "30"),
    (32, "30"),
    (33, "35"),
    (57, "55"),
    (58, "00"),    # rounds up to 60 -> minute 00 of the NEXT hour (see below)
])
def test_targets_minute_snap_to_5(minute, expected):
    """Minutes snap to the nearest 5; 58 rounds up to 60 -> :00 of next hour."""
    s = _compute_schedule_targets(datetime(2026, 5, 13, 10, minute))
    assert s["minute_value"] == expected


def test_targets_minute_snap_carries_into_hour():
    """Regression (CORR-002): snapping minute 58 up to 60 must CARRY into the
    hour, not silently land on :00 of the same hour (which published ~58 min
    early). 10:58 -> 11:00, not 10:00."""
    s = _compute_schedule_targets(datetime(2026, 5, 13, 10, 58))
    assert s["minute_value"] == "00"
    assert s["hour_value"] == "11"   # carried, not "10"
    assert s["ampm_value"] == "am"


def test_targets_minute_snap_carries_across_midnight():
    """23:58 snaps to 00:00 of the NEXT day — hour/day/month must all roll."""
    s = _compute_schedule_targets(datetime(2026, 5, 31, 23, 58))
    assert s["minute_value"] == "00"
    assert s["hour_value"] == "00"        # midnight
    assert s["ampm_value"] == "am"
    assert s["day_id"] == "id-2026-06-01"  # rolled into June
    assert s["header"] == "June 2026"


# --------- _compute_schedule_targets: tz-aware ---------

def test_targets_tz_aware_converts_to_eastern():
    """A tz-aware UTC datetime gets converted to America/New_York."""
    # 2026-05-13 16:00 UTC = 12:00 EDT (DST is in effect in May)
    utc = datetime(2026, 5, 13, 16, 0, tzinfo=timezone.utc)
    s = _compute_schedule_targets(utc)
    assert s["target"].hour == 12
    assert s["target"].minute == 0
    assert s["day_id"] == "id-2026-05-13"
    assert s["ampm_value"] == "pm"
    assert s["hour_value"] == "00"  # 12 PM displays as 12, value="00"


def test_targets_tz_aware_crosses_day_boundary():
    """A late UTC time can roll back to the previous day in Eastern."""
    # 2026-01-13 02:00 UTC = 2026-01-12 21:00 EST (no DST in January)
    utc = datetime(2026, 1, 13, 2, 0, tzinfo=timezone.utc)
    s = _compute_schedule_targets(utc)
    assert s["target"].day == 12
    assert s["day_id"] == "id-2026-01-12"
    assert s["header"] == "January 2026"
    assert s["hour_value"] == "09"
    assert s["ampm_value"] == "pm"


# --------- _parse_picker_header ---------

def test_parse_picker_header_uppercase():
    """The picker title is CSS-uppercased; parser must accept all cases."""
    assert _parse_picker_header("MAY 2026") == datetime(2026, 5, 1)
    assert _parse_picker_header("May 2026") == datetime(2026, 5, 1)
    assert _parse_picker_header("may 2026") == datetime(2026, 5, 1)


def test_parse_picker_header_strips_whitespace():
    assert _parse_picker_header("  March 2027  ") == datetime(2027, 3, 1)


def test_parse_picker_header_invalid_raises_value_error():
    with pytest.raises(ValueError):
        _parse_picker_header("not a date")
    with pytest.raises(ValueError):
        _parse_picker_header("2026-05")


# --------- _compute_month_delta ---------

@pytest.mark.parametrize("header,target,expected", [
    ("May 2026",      datetime(2026, 5, 13),  0),    # same month
    ("May 2026",      datetime(2026, 6, 1),   1),    # next month
    ("May 2026",      datetime(2026, 4, 30), -1),    # prev month
    ("May 2026",      datetime(2027, 5, 1),  12),    # next year
    ("May 2026",      datetime(2025, 5, 1), -12),    # prev year
    ("May 2026",      datetime(2027, 1, 1),   8),    # cross year forward
    ("January 2026",  datetime(2025, 12, 1), -1),    # cross year backward
    ("MAY 2026",      datetime(2026, 6, 1),   1),    # uppercase header
])
def test_compute_month_delta(header, target, expected):
    assert _compute_month_delta(header, target) == expected
