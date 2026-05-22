"""Scans network directories and parses dates from filenames."""

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class MediaDateEntry:
    date: str  # ISO string: YYYY-MM-DD
    display_date: str  # e.g. "March 26, 2021"
    youtube_video_path: Optional[str] = None
    youtube_shorts_path: Optional[str] = None
    podcast_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    email_thumbnail_path: Optional[str] = None
    date_ambiguous: bool = False
    date_alternatives: list = field(default_factory=list)


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".ogg"}
THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _load_config() -> dict:
    from core.config import load_config
    return load_config()


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


def _parse_date_from_stem(stem: str) -> Optional[datetime]:
    """Return the primary datetime parsed from a filename stem, or None.

    Thin wrapper around _parse_date_entry_from_stem() that returns only the
    primary datetime, for backward-compatibility with callers that don't need
    ambiguity information.
    """
    primary_dt, _, _ = _parse_date_entry_from_stem(stem)
    return primary_dt


def _parse_date_from_filename(filename: str) -> Optional[datetime]:
    """Wrapper: extract a date from a full filename (with extension)."""
    stem = os.path.splitext(filename)[0]
    return _parse_date_from_stem(stem)


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


def _scan_directory(directory: str, extensions: set) -> dict:
    """Scan a directory for files with given extensions.

    Returns a dict mapping ISO date string ->
        {"path": str, "alternatives": list[dict], "ambiguous": bool}

    When multiple files map to the same date (e.g. a re-export), the most
    recently modified file wins. We log a warning so the user can clean up
    the duplicate rather than have a coin-flip pick something stale.
    """
    results = {}
    if not os.path.isdir(directory):
        return results

    # L3: a TOCTOU race where the directory disappears between isdir() and
    # listdir() (e.g. USB unplug) used to surface as an unhandled
    # FileNotFoundError. Treat as "no files".
    try:
        entries = os.listdir(directory)
    except (FileNotFoundError, PermissionError, OSError):
        return results

    for filename in entries:
        ext = os.path.splitext(filename)[1].lower()
        if ext not in extensions:
            continue
        stem = os.path.splitext(filename)[0]
        primary_dt, alts, ambiguous = _parse_date_entry_from_stem(stem)
        if primary_dt is None:
            continue
        iso_date = primary_dt.strftime("%Y-%m-%d")
        full_path = str(Path(os.path.join(directory, filename)).resolve())
        try:
            mtime = os.path.getmtime(full_path)
        except OSError:
            mtime = 0.0

        existing = results.get(iso_date)
        if existing is None:
            results[iso_date] = {
                "path": full_path,
                "alternatives": alts,
                "ambiguous": ambiguous,
                "_mtime": mtime,
            }
            continue

        if mtime > existing.get("_mtime", 0.0):
            logger.warning(
                "Multiple files for %s in %s — keeping newer %s, ignoring %s",
                iso_date, directory, os.path.basename(full_path),
                os.path.basename(existing["path"]),
            )
            existing.update(path=full_path, alternatives=alts, ambiguous=ambiguous, _mtime=mtime)
        else:
            logger.warning(
                "Multiple files for %s in %s — keeping %s, ignoring older %s",
                iso_date, directory, os.path.basename(existing["path"]),
                os.path.basename(full_path),
            )

    # Strip the internal _mtime key before returning
    for entry in results.values():
        entry.pop("_mtime", None)
    return results


def _merge_ambiguity(scan_results: dict) -> dict:
    """Collect per-date ambiguity info from multiple scan results.

    Returns {iso_date: {"alternatives": list, "ambiguous": bool}}.
    Picks the first non-empty alternatives list found for each date.
    """
    ambiguity: dict = {}
    for type_results in scan_results.values():
        for iso_date, info in type_results.items():
            if iso_date not in ambiguity:
                ambiguity[iso_date] = {
                    "alternatives": info.get("alternatives", []),
                    "ambiguous": info.get("ambiguous", False),
                }
    return ambiguity


class FileScanner:
    """Scans configured directories for media files and groups them by date."""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or _load_config()
        dirs = self.config["directories"]
        self.base = dirs["base"]
        self.youtube_video_dir = os.path.join(self.base, dirs["youtube_video"])
        self.youtube_shorts_dir = os.path.join(self.base, dirs["youtube_shorts"])
        self.podcast_dir = os.path.join(self.base, dirs["podcast"])
        self.thumbnails_dir = os.path.join(self.base, dirs["thumbnails"])
        # Optional: email-specific thumbnails (YouTube-play-button overlay).
        # Older configs won't have this key; treat it as "not configured".
        _email_thumbs = dirs.get("email_thumbnails")
        self.email_thumbnails_dir = (
            os.path.join(self.base, _email_thumbs) if _email_thumbs else None
        )

    def _scan_all(self) -> tuple:
        """Scan all directories and merge results by date.

        Returns (merged_paths, ambiguity_map) where:
          merged_paths = {iso_date: {path fields…}}
          ambiguity_map = {iso_date: {"alternatives": list, "ambiguous": bool}}
        """
        scan_results = {
            "youtube_video": _scan_directory(self.youtube_video_dir, VIDEO_EXTENSIONS),
            "youtube_shorts": _scan_directory(self.youtube_shorts_dir, VIDEO_EXTENSIONS),
            "podcast": _scan_directory(self.podcast_dir, AUDIO_EXTENSIONS),
            "thumbnails": _scan_directory(self.thumbnails_dir, THUMBNAIL_EXTENSIONS),
        }
        if self.email_thumbnails_dir:
            scan_results["email_thumbnails"] = _scan_directory(
                self.email_thumbnails_dir, THUMBNAIL_EXTENSIONS
            )

        all_dates: set = set()
        for type_results in scan_results.values():
            all_dates.update(type_results.keys())

        merged: dict = {}
        for iso_date in all_dates:
            merged[iso_date] = {
                "youtube_video_path": (scan_results["youtube_video"].get(iso_date) or {}).get("path"),
                "youtube_shorts_path": (scan_results["youtube_shorts"].get(iso_date) or {}).get("path"),
                "podcast_path": (scan_results["podcast"].get(iso_date) or {}).get("path"),
                "thumbnail_path": (scan_results["thumbnails"].get(iso_date) or {}).get("path"),
                "email_thumbnail_path": (scan_results.get("email_thumbnails", {}).get(iso_date) or {}).get("path"),
            }

        ambiguity = _merge_ambiguity(scan_results)
        return merged, ambiguity

    def get_available_dates(self) -> list:
        """Return all available dates sorted newest-first."""
        merged, ambiguity = self._scan_all()
        entries = []
        for iso_date, paths in merged.items():
            # L4: defensive — _scan_all() should only ever produce valid
            # iso strings, but a malformed key would otherwise crash the
            # whole index page. Skip it instead.
            try:
                dt = datetime.strptime(iso_date, "%Y-%m-%d")
            except (ValueError, TypeError):
                continue
            display = dt.strftime("%B %d, %Y")
            amb_info = ambiguity.get(iso_date, {"alternatives": [], "ambiguous": False})
            entries.append(
                MediaDateEntry(
                    date=iso_date,
                    display_date=display,
                    date_ambiguous=amb_info["ambiguous"],
                    date_alternatives=amb_info["alternatives"],
                    **paths,
                )
            )
        entries.sort(key=lambda e: e.date, reverse=True)
        return entries

    def get_files_for_dates(self, date_list: list) -> list:
        """Return MediaDateEntry objects only for the specified dates."""
        all_entries = self.get_available_dates()
        date_set = set(date_list)
        return [e for e in all_entries if e.date in date_set]

    def scan_custom_paths(self, path_overrides: dict) -> list:
        """Scan using custom directory paths, falling back to config.yaml values.

        path_overrides keys: youtube_video, youtube_shorts, podcast, thumbnails.
        Any key present uses that path; absent keys fall back to config.yaml.
        Non-existent directories are skipped with a warning.
        """
        dir_map = {
            "youtube_video": (self.youtube_video_dir, VIDEO_EXTENSIONS),
            "youtube_shorts": (self.youtube_shorts_dir, VIDEO_EXTENSIONS),
            "podcast": (self.podcast_dir, AUDIO_EXTENSIONS),
            "thumbnails": (self.thumbnails_dir, THUMBNAIL_EXTENSIONS),
        }
        # Only offer the email-thumbnail slot when a directory is configured
        # (here or via override); absent config shouldn't fabricate a path.
        if self.email_thumbnails_dir or "email_thumbnails" in path_overrides:
            dir_map["email_thumbnails"] = (
                self.email_thumbnails_dir, THUMBNAIL_EXTENSIONS
            )

        field_names = {
            "youtube_video": "youtube_video_path",
            "youtube_shorts": "youtube_shorts_path",
            "podcast": "podcast_path",
            "thumbnails": "thumbnail_path",
            "email_thumbnails": "email_thumbnail_path",
        }

        scan_results: dict = {}
        for key, (default_dir, extensions) in dir_map.items():
            directory = path_overrides.get(key, default_dir)
            if not directory:
                logger.warning("No path configured for %s, skipping.", key)
                continue
            if not os.path.isdir(directory):
                logger.warning("Directory does not exist for %s: %s, skipping.", key, directory)
                continue
            scan_results[key] = _scan_directory(directory, extensions)

        all_dates: set = set()
        for type_results in scan_results.values():
            all_dates.update(type_results.keys())

        ambiguity = _merge_ambiguity(scan_results)

        entries = []
        for iso_date in all_dates:
            # L4: defensive parse — see get_available_dates above.
            try:
                dt = datetime.strptime(iso_date, "%Y-%m-%d")
            except (ValueError, TypeError):
                continue
            display = dt.strftime("%B %d, %Y")
            kwargs: dict = {"date": iso_date, "display_date": display}
            for key, fname in field_names.items():
                kwargs[fname] = (scan_results.get(key, {}).get(iso_date) or {}).get("path")
            amb_info = ambiguity.get(iso_date, {"alternatives": [], "ambiguous": False})
            kwargs["date_ambiguous"] = amb_info["ambiguous"]
            kwargs["date_alternatives"] = amb_info["alternatives"]
            entries.append(MediaDateEntry(**kwargs))

        entries.sort(key=lambda e: e.date, reverse=True)
        return entries

    @staticmethod
    def validate_path(path: str) -> dict:
        """Validate a directory path and return status info.

        Returns: { "exists": bool, "readable": bool, "file_count": int, "sample_files": list[str] }
        """
        result = {"exists": False, "readable": False, "file_count": 0, "sample_files": []}
        if not path:
            return result
        if not os.path.exists(path):
            return result
        result["exists"] = True
        try:
            files = os.listdir(path)
            result["readable"] = True
            result["file_count"] = len(files)
            result["sample_files"] = sorted(files)[:10]
        except (PermissionError, FileNotFoundError, OSError):
            # L5: include FileNotFoundError to cover the race where the
            # directory exists at the os.path.exists() check above but
            # disappears before listdir() (USB unplug between checks).
            result["readable"] = False
        return result
