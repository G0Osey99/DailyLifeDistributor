"""Scan configured local media folders into an available-dates report.

Reuses core.file_scanner.parse_names (pure, no-DB, no-Flask) to map filenames
to ISO dates. The agent owns filesystem access; the browser only renders the
report this produces. Top-level files per root are scanned (one folder per
category, matching how the dashboard pickers are organized).

The module also exposes a path-resolution layer used by agent/dispatch.py:

    scan.set_roots(roots)           # configure once (or override in tests)
    result = scan.scan()            # -> {iso: {kind: full_path}}
    result = scan.latest_results()  # -> same dict, cached from last scan()

Media-kind mapping (category key -> kind name in the result dict):
    "video"           -> "video"          (YouTube long-form + Rock)
    "thumbnail"       -> "thumbnail"      (YouTube + Rock thumbnail)
    "short_video"     -> "short_video"    (YouTube Shorts)
    "short_thumbnail" -> "short_thumbnail"
    "audio"           -> "audio"          (Simplecast / podcast)
    "spotlight"       -> "spotlight"      (Rock spotlight image)
    "vista"           -> "vista"          (Rock vista image)
    "reflection"      -> "reflection"     (Rock reflection image)
    "email_thumbnail" -> "email_thumbnail"(Rock Email separate thumbnail dir)
"""
from __future__ import annotations

import os
import threading

from core.file_scanner import parse_names

# ---------------------------------------------------------------------------
# Category → media-kind mapping
# The keys match what callers store in agent config / set_roots().
# The values are the kind strings used in the paths dict returned by scan().
# ---------------------------------------------------------------------------
_KIND_MAP: dict[str, str] = {
    "video":           "video",
    "thumbnail":       "thumbnail",
    "short_video":     "short_video",
    "short_thumbnail": "short_thumbnail",
    "audio":           "audio",
    "spotlight":       "spotlight",
    "vista":           "vista",
    "reflection":      "reflection",
    "email_thumbnail": "email_thumbnail",
}

# ---------------------------------------------------------------------------
# Module-level state: configured roots + last scan cache
# ---------------------------------------------------------------------------
_roots: dict[str, str] = {}
_roots_lock = threading.Lock()

_last_results: dict[str, dict[str, str]] = {}   # iso -> {kind: full_path}
_last_lock = threading.Lock()


def set_roots(roots: dict[str, str]) -> None:
    """Configure the media-directory roots used by scan() and latest_results().

    `roots` maps category name (e.g. "video", "email_thumbnail") to an
    absolute directory path. Called from agent/config or tests.
    """
    with _roots_lock:
        _roots.clear()
        _roots.update(roots)


def get_roots() -> dict[str, str]:
    with _roots_lock:
        return dict(_roots)


def scan(*, roots: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    """Scan media directories and return {iso: {kind: full_path}}.

    For each date, only the FIRST matching file per kind is recorded
    (alphabetical order within the directory listing).

    The result is cached and retrievable via latest_results().

    Args:
        roots: override for testing; if None uses the module-level roots.
    """
    effective_roots = roots if roots is not None else get_roots()
    result: dict[str, dict[str, str]] = {}

    for category, dir_path in effective_roots.items():
        kind = _KIND_MAP.get(category, category)   # unknown categories pass through
        try:
            names = sorted(
                n for n in os.listdir(dir_path)
                if os.path.isfile(os.path.join(dir_path, n))
            )
        except OSError:
            continue
        for iso, files in parse_names(names).items():
            if files and kind not in result.setdefault(iso, {}):
                result[iso][kind] = os.path.join(dir_path, files[0])

    with _last_lock:
        _last_results.clear()
        _last_results.update(result)

    return result


def latest_results() -> dict[str, dict[str, str]]:
    """Return the cached result from the most recent scan() call.

    Returns an empty dict if scan() has never been called.
    Used by agent/dispatch.py to resolve paths without re-scanning.
    """
    with _last_lock:
        return dict(_last_results)


def scan_roots(roots: dict) -> dict:
    """roots maps category -> directory path. Returns:

        {"by_date": {iso: {category: [filename, ...]}},
         "dates":   [iso, ...] sorted ascending,
         "errors":  {category: message}}   # unreadable/missing dirs
    """
    by_date: dict[str, dict[str, list]] = {}
    errors: dict[str, str] = {}

    for category, path in roots.items():
        try:
            names = [n for n in os.listdir(path)
                     if os.path.isfile(os.path.join(path, n))]
        except OSError as e:
            errors[category] = str(e)
            continue
        for iso, files in parse_names(names).items():
            by_date.setdefault(iso, {})[category] = files

    return {
        "by_date": by_date,
        "dates": sorted(by_date.keys()),
        "errors": errors,
    }
