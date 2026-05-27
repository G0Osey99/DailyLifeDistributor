"""Parse dates from media filenames (browser-streaming pipeline).

The browser reports the filenames in each picked folder; ``parse_names``
groups them by date with no filesystem access, reusing the multi-format date
extraction below.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class MediaDateEntry:
    date: str  # ISO string: YYYY-MM-DD
    display_date: str  # e.g. "March 26, 2021"
    youtube_video_path: Optional[str] = None
    youtube_shorts_path: Optional[str] = None
    podcast_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    email_thumbnail_path: Optional[str] = None
    # Original filename of the shorts video as it appeared on the user's
    # disk (e.g. "app 260601.mp4"), used by session_state.build_entry to
    # infer the Wistia media-reference label. The web path reassembles
    # chunks into a hex-UUID file_id with no trace of the original name,
    # so ``infer_wistia_ref(temp_path)`` returns "" — surfaces as
    # "Missing required fields: wistia_ref" when Rock Spotlight runs.
    youtube_shorts_name: Optional[str] = None
    date_ambiguous: bool = False
    date_alternatives: list = field(default_factory=list)


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".ogg"}
THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


# ---------------------------------------------------------------------------
# Individual format parsers — validate month (1-12) and day (1-31) explicitly.
# 2-digit years always interpret as 20YY (2000+yy).
# ---------------------------------------------------------------------------

def _valid(dt: datetime) -> Optional[datetime]:
    """Return dt if year is sane and date fields are within normal ranges."""
    if dt is None:
        return None
    if not (1990 <= dt.year <= 2099):
        return None
    if not (1 <= dt.month <= 12):
        return None
    if not (1 <= dt.day <= 31):
        return None
    return dt


def _try_mmdd(digits: str) -> Optional[datetime]:
    """Parse 4-digit string as MMDD, inferring the year from context.

    Uses the current year; if the resulting date is more than 60 days in the
    past, advances to next year (handles scheduling content near year-end).
    """
    try:
        mm, dd = int(digits[0:2]), int(digits[2:4])
        today = datetime.today()
        dt = _valid(datetime(today.year, mm, dd))
        if dt is None:
            return None
        # If the date is more than 60 days in the past, assume next year
        if (today - dt).days > 60:
            dt = _valid(datetime(today.year + 1, mm, dd))
        return dt
    except ValueError:
        return None


def _try_mdd(digits: str) -> Optional[datetime]:
    """Parse 3-digit string as M-DD (single-digit month + 2-digit day).

    Handles the "missing leading zero" case — e.g. ``602.jpg`` for June 2,
    where the operator dropped the leading zero on the month. Only the
    month is ambiguous; the day is always 2 digits so we don't have to
    consider M-D / MM-D interpretations.

    Same year-inference rule as ``_try_mmdd``: assume current year, but
    bump to next year if the resulting date is more than 60 days in the
    past (handles scheduling content near year-end).
    """
    try:
        mm, dd = int(digits[0:1]), int(digits[1:3])
        today = datetime.today()
        dt = _valid(datetime(today.year, mm, dd))
        if dt is None:
            return None
        if (today - dt).days > 60:
            dt = _valid(datetime(today.year + 1, mm, dd))
        return dt
    except ValueError:
        return None


def _try_yymmdd(digits: str) -> Optional[datetime]:
    """Parse 6-digit string as YYMMDD, always interpreting YY as 20YY."""
    try:
        yy, mm, dd = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
        return _valid(datetime(2000 + yy, mm, dd))
    except ValueError:
        return None


def _try_ddmmyy(digits: str) -> Optional[datetime]:
    """Parse 6-digit string as DDMMYY, always interpreting YY as 20YY."""
    try:
        dd, mm, yy = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
        return _valid(datetime(2000 + yy, mm, dd))
    except ValueError:
        return None


def _try_ddmmyyyy(digits: str) -> Optional[datetime]:
    """Parse 8-digit string as DDMMYYYY."""
    try:
        return _valid(datetime.strptime(digits, "%d%m%Y"))
    except ValueError:
        return None


def _try_yyyymmdd(digits: str) -> Optional[datetime]:
    """Parse 8-digit string as YYYYMMDD."""
    try:
        return _valid(datetime.strptime(digits, "%Y%m%d"))
    except ValueError:
        return None


def _is_plausible(dt: datetime) -> bool:
    """Return True if the date is within the 5-year active media window.

    Discards dates whose year is more than 5 years before the current year
    (i.e. dt.year <= today.year - 5).  Future dates and dates within the
    5-year window are considered plausible for scheduled/recent content.
    """
    return dt.year > datetime.today().year - 5


def _parse_6digit_with_alternatives(digits: str) -> tuple:
    """For exactly 6 digits try YYMMDD and DDMMYY, apply plausibility filter.

    Returns (primary_dt, alternatives, is_ambiguous) where:
      - alternatives is a list of dicts:
          {"date": "YYYY-MM-DD", "display": "Month DD, YYYY",
           "interpretation": "YYMMDD"|"DDMMYY"}
      - primary_dt is the datetime for the YYMMDD interpretation if plausible,
        otherwise the first remaining plausible interpretation (or None).
      - is_ambiguous is True when more than one plausible interpretation exists.
    """
    candidates = []

    yymmdd_dt = _try_yymmdd(digits)
    if yymmdd_dt and _is_plausible(yymmdd_dt):
        candidates.append({"dt": yymmdd_dt, "interpretation": "YYMMDD"})

    ddmmyy_dt = _try_ddmmyy(digits)
    if ddmmyy_dt and _is_plausible(ddmmyy_dt):
        candidates.append({"dt": ddmmyy_dt, "interpretation": "DDMMYY"})

    if not candidates:
        return None, [], False

    # Primary preference: YYMMDD; fall back to first valid candidate.
    primary_cand = next(
        (c for c in candidates if c["interpretation"] == "YYMMDD"),
        candidates[0],
    )
    primary_dt = primary_cand["dt"]

    alternatives = [
        {
            "date": c["dt"].strftime("%Y-%m-%d"),
            "display": c["dt"].strftime("%B %d, %Y"),
            "interpretation": c["interpretation"],
        }
        for c in candidates
    ]
    is_ambiguous = len(candidates) > 1
    return primary_dt, alternatives, is_ambiguous


def _parse_date_entry_from_stem(stem: str) -> tuple:
    """Return (primary_dt, alternatives, is_ambiguous) for a filename stem.

    For 6-digit strings, applies plausibility filtering and ambiguity detection.
    For 8-digit strings, uses DDMMYYYY / YYYYMMDD with no ambiguity check.
    For longer strings, scans substrings using the same priority order.
    Returns (None, [], False) if no date can be parsed.
    """
    digits = re.sub(r"\D", "", stem)
    if not digits:
        return None, [], False

    if len(digits) == 3:
        # M-DD (missing leading zero on month) — e.g. `602.jpg` for June 2.
        # No alternatives to consider: the day is always 2 digits.
        dt = _try_mdd(digits)
        return dt, [], False

    if len(digits) == 4:
        dt = _try_mmdd(digits)
        return dt, [], False

    if len(digits) == 6:
        return _parse_6digit_with_alternatives(digits)

    if len(digits) == 8:
        dt = _try_ddmmyyyy(digits) or _try_yyyymmdd(digits)
        return dt, [], False

    # Longer digit strings: scan 8-digit substrings FIRST. 8-digit hits are
    # unambiguous (DDMMYYYY / YYYYMMDD), so when a stem contains both an
    # 8-digit date and incidental 6-digit runs (e.g. version numbers,
    # encoded ids), we should trust the 8-digit one. Falling through to
    # 6-digit only when no 8-digit window parses keeps the previous
    # behaviour for plain YYMMDD-prefixed filenames.
    for i in range(len(digits) - 7):
        chunk = digits[i:i + 8]
        dt = _try_ddmmyyyy(chunk) or _try_yyyymmdd(chunk)
        if dt:
            return dt, [], False

    for i in range(len(digits) - 5):
        chunk = digits[i:i + 6]
        primary_dt, alts, ambiguous = _parse_6digit_with_alternatives(chunk)
        if primary_dt:
            return primary_dt, alts, ambiguous

    return None, [], False


_MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | THUMBNAIL_EXTENSIONS


def parse_names(names: list) -> dict:
    """Group a list of *filenames* by ISO date, no filesystem access.

    The browser reports the filenames inside each picked folder; this returns
    ``{iso_date: [filename, ...]}`` reusing the same per-stem date parsing as
    the directory scanner. Non-media files (extension not in the media
    allowlist) and undated names are ignored — this covers the ``.DS_Store``
    and ``.txt`` junk that ``webkitdirectory`` includes.

    Ambiguous 6-digit dates (both YYMMDD and DDMMYY plausible) surface the
    file under *each* candidate date so the user can pick the right one,
    mirroring the directory scanner's ``alternatives`` behaviour.
    """
    out: dict = {}
    for name in names:
        if not name:
            continue
        base = os.path.basename(name)
        ext = os.path.splitext(base)[1].lower()
        if ext not in _MEDIA_EXTENSIONS:
            continue
        stem = os.path.splitext(base)[0]
        primary_dt, alts, ambiguous = _parse_date_entry_from_stem(stem)
        if primary_dt is None:
            continue
        if ambiguous and alts:
            iso_dates = [a["date"] for a in alts]
        else:
            iso_dates = [primary_dt.strftime("%Y-%m-%d")]
        for iso in iso_dates:
            out.setdefault(iso, [])
            if base not in out[iso]:
                out[iso].append(base)
    return out


