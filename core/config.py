"""Project paths and config.yaml access.

Centralizes the constants and helpers that previously lived at the top of
app.py. Anything that needs to read config or know where the project root
is should import from here.
"""
from __future__ import annotations

import copy
import os
import threading

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")


# mtime-keyed cache. Several routes call load_config() multiple times per
# request and the YAML parse showed up as a hotspot. We keep the cached dict
# as a deep-copy source: callers sometimes mutate the returned dict (e.g.
# settings save) and we don't want those mutations to leak across requests.
_CACHE_LOCK = threading.Lock()
_CACHE: dict | None = None
_CACHE_MTIME: float | None = None


def load_config() -> dict:
    global _CACHE, _CACHE_MTIME
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        mtime = None
    with _CACHE_LOCK:
        if _CACHE is not None and _CACHE_MTIME == mtime:
            return copy.deepcopy(_CACHE)
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _CACHE = data
        _CACHE_MTIME = mtime
        return copy.deepcopy(_CACHE)


def invalidate_config_cache() -> None:
    """Force the next load_config() to re-read from disk."""
    global _CACHE, _CACHE_MTIME
    with _CACHE_LOCK:
        _CACHE = None
        _CACHE_MTIME = None


def default_platforms(config: dict) -> dict:
    platforms_cfg = config.get("platforms", {})
    return {
        "youtube_video": platforms_cfg.get("youtube_video", True),
        "youtube_shorts": platforms_cfg.get("youtube_shorts", True),
        "simplecast": platforms_cfg.get("simplecast", True),
        "rock": platforms_cfg.get("rock", False),
    }


def resolved_dirs(config: dict) -> dict:
    """Return resolved directory paths from config.yaml."""
    dirs = config.get("directories", {})
    base = dirs.get("base", "")
    return {
        "youtube_video": os.path.join(base, dirs.get("youtube_video", "")),
        "youtube_shorts": os.path.join(base, dirs.get("youtube_shorts", "")),
        "podcast": os.path.join(base, dirs.get("podcast", "")),
        "thumbnails": os.path.join(base, dirs.get("thumbnails", "")),
    }


def allowed_path_roots(config: dict | None = None) -> list[str]:
    """Roots that client-supplied paths may live under.

    Used by /browse, /scan, /validate-path, and the /settings/excel-* endpoints
    to keep someone "fooling around" from listing or reading arbitrary parts
    of the filesystem (e.g. C:\\Windows, /etc, ~/.ssh). The user's own home
    directory and the configured media root are always allowed; the project
    root is allowed so the user can browse the USB drive itself.
    """
    if config is None:
        try:
            config = load_config()
        except Exception:
            config = {}
    roots: list[str] = []

    def _add(p: str | None) -> None:
        if not p:
            return
        try:
            real = os.path.realpath(p)
        except OSError:
            return
        if real and real not in roots:
            roots.append(real)

    _add(os.path.expanduser("~"))
    _add(PROJECT_ROOT)
    dirs = (config or {}).get("directories", {}) or {}
    _add(dirs.get("base"))
    return roots


def is_path_allowed(path: str, config: dict | None = None) -> bool:
    """Return True iff *path* resolves under one of the allowed roots."""
    if not path or not isinstance(path, str):
        return False
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    for root in allowed_path_roots(config):
        try:
            common = os.path.commonpath([real, root])
        except ValueError:
            # Different drives on Windows raise ValueError.
            continue
        if common == root:
            return True
    return False
