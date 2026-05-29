"""Agent-side media-folder configuration — the bridge between the user's
local disk layout and the scanner's kind-keyed path map.

Why this exists
---------------
The agent uploads from the user's own machine, so it must know WHERE the
media lives locally. The web dashboard solves this with per-run folder
pickers in the browser; the agent solves it ONCE, persistently, because
it's a long-lived per-OS app. Before this module nothing wired the saved
folders into ``agent.scan``, so ``scan.scan()`` returned nothing and every
agent-path upload failed with file-not-found.

The five categories mirror the dashboard's folder pickers exactly so the
user configures the same five folders they already know. Each maps to one
or more scanner "kinds" — the keys ``agent/run_batch._dispatch_upload``
reads out of the per-date path map:

    config key        GUI label                       feeds scan kind(s)
    ----------------  ------------------------------  -----------------------
    video             Horizontal Video (YouTube)      video
    short_video       Vertical Video (Shorts)         short_video
    audio             Podcast Audio (SimpleCast)      audio
    thumbnail         Thumbnails (YouTube + Rock)      thumbnail, short_thumbnail
    email_thumbnail   Email Thumbnails (Rock Email)    email_thumbnail

The one non-obvious mapping is Thumbnails → BOTH ``thumbnail`` and
``short_thumbnail``: the dispatch reads ``short_thumbnail`` for the Shorts
row but the web path shares a single thumbnails folder for video + Shorts,
so we duplicate the one folder onto both kinds to match that behavior.

"Universal" config: the persistence (``agent/config.py``) is plain JSON in
the user's home dir, and the GUI picker uses ``tkinter.filedialog`` — both
work identically on Windows / macOS / Linux. :func:`autodetect_roots` adds a
one-click path: point at a single parent folder and it matches the five
subfolders by name.
"""
from __future__ import annotations

import logging
import os

_log = logging.getLogger(__name__)

# (config_key, human label, [scan kinds it feeds]). Order = GUI display order.
MEDIA_CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("video",           "Horizontal Video (YouTube)",    ["video"]),
    ("short_video",     "Vertical Video (Shorts)",       ["short_video"]),
    ("audio",           "Podcast Audio (SimpleCast)",    ["audio"]),
    ("thumbnail",       "Thumbnails (YouTube + Rock)",    ["thumbnail", "short_thumbnail"]),
    ("email_thumbnail", "Email Thumbnails (Rock Email)", ["email_thumbnail"]),
]

# Config keys in canonical order — handy for the GUI + validation.
CONFIG_KEYS: list[str] = [c[0] for c in MEDIA_CATEGORIES]


def scan_roots_from_config(saved: dict | None) -> dict[str, str]:
    """Expand the saved ``{config_key: path}`` map into ``{scan_kind: path}``.

    Only keys present (and truthy) in *saved* contribute. The Thumbnails
    folder fans out to both ``thumbnail`` and ``short_thumbnail`` so the
    agent's Shorts row gets a thumbnail the same way the web path does.
    Unknown keys in *saved* are ignored (forward/back compatibility).
    """
    saved = saved or {}
    out: dict[str, str] = {}
    for key, _label, kinds in MEDIA_CATEGORIES:
        path = saved.get(key)
        if path:
            for kind in kinds:
                out[kind] = path
    return out


# Auto-detect keyword table: each (keywords, config_key). A subfolder whose
# lowercased name contains ANY keyword maps to that category. Order matters —
# "email thumbnail" must match email_thumbnail BEFORE the generic thumbnail
# rule, so email is listed first.
_AUTODETECT: list[tuple[tuple[str, ...], str]] = [
    (("horizontal", "landscape", "wide", "16x9", "16:9"), "video"),
    (("short", "vertical", "reel", "9x16", "9:16"), "short_video"),
    (("podcast", "audio", "episode"), "audio"),
    (("email",), "email_thumbnail"),
    (("thumbnail", "thumb", "cover", "artwork"), "thumbnail"),
]


def _match_category(folder_name: str) -> str | None:
    low = folder_name.lower()
    for keywords, key in _AUTODETECT:
        if any(kw in low for kw in keywords):
            return key
    return None


def autodetect_roots(parent_dir: str) -> dict[str, str]:
    """Scan *parent_dir*'s immediate subfolders and map them to categories
    by name. Returns ``{config_key: abspath}`` for every category that
    matched exactly one subfolder.

    The "universal" one-click path: the user points at one parent folder
    (e.g. ``~/DailyLife/2026-06``) and we figure out the five subfolders
    instead of making them pick each one. Ambiguous matches (two subfolders
    hitting the same category) are left unset so the user resolves them by
    hand — we never guess between two candidates.
    """
    try:
        entries = sorted(os.listdir(parent_dir))
    except OSError as e:
        _log.warning("autodetect_roots: cannot list %r: %s", parent_dir, e)
        return {}
    # Collect candidate folders per category, then keep only unambiguous ones.
    candidates: dict[str, list[str]] = {}
    for name in entries:
        full = os.path.join(parent_dir, name)
        if not os.path.isdir(full):
            continue
        key = _match_category(name)
        if key:
            candidates.setdefault(key, []).append(full)
    out: dict[str, str] = {}
    for key, paths in candidates.items():
        if len(paths) == 1:
            out[key] = paths[0]
        else:
            _log.info("autodetect_roots: %d candidates for %r (%s) — skipping, "
                      "user must choose", len(paths), key, paths)
    return out


def apply_saved_roots() -> dict[str, str]:
    """Load the saved media roots from config and push them into the scanner.

    Returns the resolved ``{scan_kind: path}`` map (also useful for logging
    "configured N folders"). Safe to call at startup even when nothing is
    configured yet — it just sets an empty root map.
    """
    from agent import config, scan
    saved = config.get_media_roots()
    roots = scan_roots_from_config(saved)
    scan.set_roots(roots)
    _log.info("media roots applied: %d folder(s) configured (%s)",
              len([k for k in CONFIG_KEYS if (saved or {}).get(k)]),
              ", ".join(sorted(set(roots.values()))) or "none")
    return roots


def save_and_apply(saved: dict) -> dict[str, str]:
    """Persist *saved* (``{config_key: path}``) and apply it live.

    Called by the GUI after the user picks folders so the change takes
    effect for the next job without an agent restart.
    """
    from agent import config
    # Keep only known keys with truthy paths — don't persist stray entries.
    clean = {k: saved[k] for k in CONFIG_KEYS if saved.get(k)}
    config.set_media_roots(clean)
    return apply_saved_roots()
