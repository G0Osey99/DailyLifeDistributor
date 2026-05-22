"""Scan configured local media folders into an available-dates report.

Reuses core.file_scanner.parse_names (pure, no-DB, no-Flask) to map filenames
to ISO dates. The agent owns filesystem access; the browser only renders the
report this produces. Top-level files per root are scanned (one folder per
category, matching how the dashboard pickers are organized)."""
from __future__ import annotations

import os

from core.file_scanner import parse_names


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
