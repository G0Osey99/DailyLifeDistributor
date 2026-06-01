"""Project paths and config.yaml access.

Centralizes the constants and helpers that previously lived at the top of
app.py. Anything that needs to read config or know where the project root
is should import from here.
"""
from __future__ import annotations

import copy
import logging
import os
import threading

# NOTE: PyYAML is imported lazily inside load_config(), NOT at module level.
# The bundled hybrid agent (agent/requirements.txt) does not ship PyYAML and
# has no config.yaml on disk — it gets its config from the job envelope. But
# core.session_state instantiates its SessionState singleton at import time,
# which calls load_config(); a top-level `import yaml` therefore crashed the
# agent's whole dispatch path with ModuleNotFoundError the moment it imported
# session_state to build a ReviewEntry. Keeping the import lazy lets the module
# import cleanly without PyYAML, and load_config() degrades to {} when there's
# no config file (the agent's case) instead of needing the parser at all.

_log = logging.getLogger(__name__)

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
        data: dict = {}
        if mtime is not None:
            # config.yaml exists → parse it. yaml is imported here (lazily) so
            # environments without PyYAML and without a config file (the
            # bundled agent) never need the parser.
            try:
                import yaml
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            except FileNotFoundError:
                data = {}  # raced: deleted between stat and open
            except ModuleNotFoundError:
                # config.yaml present but PyYAML missing — surface it (a real
                # misconfiguration on a server) but don't hard-crash callers
                # that only need defaults.
                _log.warning("config.yaml present but PyYAML is not installed; "
                             "using empty config defaults")
                data = {}
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
