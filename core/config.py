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


def effective_config(org_id: int | None = None) -> dict:
    """Return the config dict with *org_id*'s overrides merged on top.

    Overlay rules: each section from ``core.org_settings`` (currently
    ``scheduling`` and ``description_footers``) replaces matching keys on
    the global ``config.yaml`` subtree. Other keys at the same path are
    preserved. ``org_id=None`` returns ``load_config()`` verbatim — used
    by the legacy single-tenant USB install + any code path with no
    request context.

    The overlay is shallow per section: ``{"scheduling": {"youtube_video":
    "08:00"}}`` overrides only that one time while preserving the rest
    of the global scheduling subtree (timezone, other platforms).
    """
    cfg = load_config()
    if org_id is None:
        return cfg
    try:
        from core import org_settings as _os
    except Exception:  # pragma: no cover — defensive, the import always succeeds
        return cfg
    for section in ("scheduling", "description_footers"):
        overlay = _os.get_section(int(org_id), section)
        if not overlay:
            continue
        cfg.setdefault(section, {})
        cfg[section].update(overlay)
    return cfg


def default_platforms(config: dict) -> dict:
    platforms_cfg = config.get("platforms", {})
    return {
        "youtube_video": platforms_cfg.get("youtube_video", True),
        "youtube_shorts": platforms_cfg.get("youtube_shorts", True),
        "simplecast": platforms_cfg.get("simplecast", True),
        "rock": platforms_cfg.get("rock", False),
    }


# Server-side directory resolution + path-allowlist helpers were removed with
# the browser-streaming pipeline: media now lives on the user's machine and is
# streamed up per run, so there are no server directories to resolve or gate.
