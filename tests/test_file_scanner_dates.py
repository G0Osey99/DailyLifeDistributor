"""Date-parsing tests for core/file_scanner.

Covers the multi-format digit extraction, 6-digit ambiguity handling, and
plausibility filtering. The scanner reads filenames from disk so there's no
file IO needed here — we exercise the pure parser helpers directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta


from core.file_scanner import (
    _parse_date_entry_from_stem,
    _try_yymmdd,
    _try_ddmmyy,
    _try_ddmmyyyy,
    _try_yyyymmdd,
    _try_mmdd,
)


def test_yymmdd_unambiguous():
    """5-digit year prefix that can only be YYMMDD picks YYMMDD."""
    # 251231 → 2025-12-31 by YYMMDD; DDMMYY would be 25/12/31 = invalid month 12 (ok), day 31 (ok), but...
    # actually both parse as valid here. Use a stem where DDMMYY fails:
    # 250215 → YYMMDD = 2025-02-15 (valid), DDMMYY = 25-02-15 (also valid).
    # Pick one where DDMMYY is invalid: digits starting with 32 → 32 is not a valid day.
    primary, alts, ambiguous = _parse_date_entry_from_stem("video_300615")
    # 300615: YYMMDD = 2030-06-15 (valid, plausible if within 5y... 2030 > 2026-5=2021, yes)
    # DDMMYY = 30-06-15 = 2015-06-15 (year 2015 < 2021, NOT plausible)
    assert primary is not None
    assert primary.year == 2030
    assert ambiguous is False  # only one plausible interpretation


def test_yymmdd_and_ddmmyy_both_plausible_marks_ambiguous():
    """When both YYMMDD and DDMMYY produce plausible dates, ambiguity is flagged."""
    # 060312 → YYMMDD = 2006-03-12 (year 2006, far out of 5y window — not plausible),
    # but with a higher YY: 250312 → YYMMDD = 2025-03-12, DDMMYY = 25-03-12 = 2012-03-12 (year 2012, not plausible).
    # We need something where YY interpretation as either prefix or suffix is recent.
    # 250125 → YYMMDD = 2025-01-25, DDMMYY = 25-01-25 = 2025-01-25 (same!) — uninteresting.
    # 230423 → YYMMDD = 2023-04-23, DDMMYY = 23-04-23 = 2023-04-23 (same).
    # Try 240625 → YYMMDD = 2024-06-25, DDMMYY = 24-06-25 = 2025-06-24. Both within 5 years.
    primary, alts, ambiguous = _parse_date_entry_from_stem("clip_240625")
    assert primary is not None
    assert primary == datetime(2024, 6, 25)  # YYMMDD wins as primary
    assert ambiguous is True
    assert len(alts) == 2
    interpretations = {a["interpretation"] for a in alts}
    assert interpretations == {"YYMMDD", "DDMMYY"}


def test_ddmmyyyy_8digit():
    assert _try_ddmmyyyy("15032025") == datetime(2025, 3, 15)


def test_yyyymmdd_8digit():
    assert _try_yyyymmdd("20250315") == datetime(2025, 3, 15)


def test_invalid_8digit_returns_none():
    """Either 8-digit interpretation must validate to win."""
    # 13/13/2025 invalid for both DDMMYYYY and YYYYMMDD.
    assert _try_ddmmyyyy("13132025") is None
    # 20251332 fails strptime YYYYMMDD.
    assert _try_yyyymmdd("20251332") is None


def test_8digit_takes_priority_over_6digit_substrings():
    """Mixed digit run prefers the unambiguous 8-digit window."""
    # 20251215_v240115 — should use 2025-12-15 (8-digit YYYYMMDD), not the
    # trailing 6-digit 240115 → 2024-01-15.
    primary, _, _ = _parse_date_entry_from_stem("clip_20251215_v240115")
    assert primary == datetime(2025, 12, 15)


def test_no_digits_returns_none():
    primary, alts, ambiguous = _parse_date_entry_from_stem("just_a_name")
    assert primary is None
    assert alts == []
    assert ambiguous is False


def test_mmdd_uses_current_year_when_recent():
    """4-digit MMDD anchors to the current calendar year."""
    today = datetime.today()
    # Pick a date 10 days in the future so it's clearly current-year-eligible.
    future = today + timedelta(days=10)
    digits = future.strftime("%m%d")
    parsed = _try_mmdd(digits)
    assert parsed is not None
    assert parsed.year == today.year
    assert parsed.month == future.month
    assert parsed.day == future.day


def test_invalid_month_yymmdd():
    """Month 13 must be rejected by _try_yymmdd."""
    assert _try_yymmdd("251301") is None


def test_invalid_day_ddmmyy():
    """Day 32 must be rejected by _try_ddmmyy."""
    assert _try_ddmmyy("321001") is None
