"""Navbar YouTube-auth state cache.

Extracted from app.py (ARCH-002) so the settings blueprint can import the
cache helpers without importing the app factory module — that backwards
`from app import ...` was a circular dependency that only worked by import
ordering. Keeping the cache here lets both app.py and blueprints/settings.py
import from a leaf module, and makes the cache testable without a full app.

Behavior is unchanged from the original app.py implementation.
"""
from __future__ import annotations

import logging
import time

from uploaders.youtube_uploader import is_authenticated as yt_is_authenticated

# Cache yt_is_authenticated() — the navbar context processor runs on every
# render. The cache key is the effective org id (impersonation-aware) so a
# program owner acting as a tenant with no YT token sees the right state.
_YT_AUTH_CACHE: dict = {}
_YT_AUTH_TTL_SEC = 30.0  # Navbar hint only: a cleared/added token can take up to this long to reflect.


def cached_yt_authenticated() -> bool:
    """Cache the YouTube-authenticated state for the navbar, per effective org.

    The token now lives in the encrypted store (no file mtime to watch), so
    we re-check at most every _YT_AUTH_TTL_SEC seconds rather than per request.
    The cache key is the effective org id (impersonation-aware) so the
    navbar reflects the org you're currently acting as.
    """
    try:
        from core.org_context import effective_org_id
        key = effective_org_id()
    except Exception:
        key = None
    if key is None:
        key = "__no_org__"
    entry = _YT_AUTH_CACHE.get(key)
    now = time.monotonic()
    if entry is not None and (now - entry["checked_at"]) < _YT_AUTH_TTL_SEC:
        return entry["value"]
    try:
        val = bool(yt_is_authenticated())
    except Exception:
        logging.getLogger(__name__).debug(
            "cached_yt_authenticated failed; treating as unauthenticated", exc_info=True
        )
        val = False
    _YT_AUTH_CACHE[key] = {"value": val, "checked_at": now}
    return val


def invalidate_yt_auth_cache() -> None:
    """Drop the cached YouTube-auth state so the next read re-checks the store.

    Called right after the token changes (OAuth success, Clear Token) so the
    Settings badge flips immediately instead of lagging up to the TTL.

    The cache is keyed by effective org id (cached_yt_authenticated stores
    _YT_AUTH_CACHE[org_key] = {"value":.., "checked_at":..}). The previous
    implementation set sibling "value"/"checked_at" keys, which no reader ever
    consults — so it was a no-op and the badge lagged the full TTL. Clear every
    org entry so the next read for any org re-checks the store.
    """
    _YT_AUTH_CACHE.clear()


# Backward-compatible alias: app.py historically exposed this name.
_cached_yt_authenticated = cached_yt_authenticated
