"""Startup validation for environment variables.

Called once from create_app(). Warns on missing optional keys (so the user
knows which integrations will silently no-op) and raises on malformed
numeric values (so a typo doesn't blow up later inside an upload).
"""
from __future__ import annotations

import logging
import os
from typing import Iterable

log = logging.getLogger(__name__)

# (name, integration label) — empty/missing logs a single warning.
_OPTIONAL_KEYS: tuple[tuple[str, str], ...] = (
    ("UNSPLASH_ACCESS_KEY", "Unsplash image gather"),
    ("PEXELS_API_KEY", "Pexels image gather"),
)

# Numeric env vars that, if set, must parse as int. Bad value = fail fast.
_NUMERIC_KEYS: tuple[str, ...] = (
    "ROCK_LOGIN_TIMEOUT",
    "SIMPLECAST_LOGIN_TIMEOUT",
    "VISTA_SOCIAL_LOGIN_TIMEOUT",
    "MAX_CONTENT_LENGTH_BYTES",
)


def _is_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes")


def validate_env() -> None:
    _warn_missing(_OPTIONAL_KEYS)
    _check_numeric(_NUMERIC_KEYS)
    _check_youtube_secrets()
    _check_hosted_requirements()


def _check_hosted_requirements() -> None:
    """In a hosted (public) deploy, a few env vars are not optional.

    Without ``FLASK_SECRET_KEY`` the app falls back to a random key that
    changes on every restart — every session is silently invalidated on each
    redeploy. That's a tolerable annoyance for local dev but a real defect in
    production, so fail closed when ``HOSTED`` is set. ``ALLOWED_HOSTS`` backs
    the DNS-rebind guard; warn loudly if it's missing on a public box.
    """
    if not _is_truthy("HOSTED"):
        return
    if not (os.environ.get("FLASK_SECRET_KEY") or "").strip():
        raise RuntimeError(
            "HOSTED=true but FLASK_SECRET_KEY is not set. A public deploy must "
            "pin a stable secret key (otherwise sessions reset on every "
            "restart). Generate one with: python -c \"import secrets; "
            "print(secrets.token_hex(32))\" and set FLASK_SECRET_KEY."
        )
    if not (os.environ.get("ALLOWED_HOSTS") or "").strip():
        log.warning(
            "HOSTED=true but ALLOWED_HOSTS is empty; the DNS-rebind / Host-"
            "spoofing guard is disabled. Set ALLOWED_HOSTS to your public host."
        )


def _warn_missing(keys: Iterable[tuple[str, str]]) -> None:
    for name, label in keys:
        if not (os.environ.get(name) or "").strip():
            log.warning("%s is not set; %s will be unavailable.", name, label)


def _check_numeric(keys: Iterable[str]) -> None:
    bad: list[str] = []
    for name in keys:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        try:
            int(raw)
        except ValueError:
            bad.append(f"{name}={raw!r}")
    if bad:
        raise RuntimeError(
            "Invalid integer value(s) in environment: " + ", ".join(bad)
        )


def _check_youtube_secrets() -> None:
    """YOUTUBE_CLIENT_SECRETS_PATH points to a file the OAuth flow needs.

    A missing file is not fatal at startup (the user may not be uploading
    to YouTube this session), but warning early beats a confusing OAuth
    failure 20 minutes into a workflow.
    """
    path = (os.environ.get("YOUTUBE_CLIENT_SECRETS_PATH") or "client_secrets.json").strip()
    if not os.path.isabs(path):
        from core.config import PROJECT_ROOT
        path = os.path.join(PROJECT_ROOT, path)
    if not os.path.exists(path):
        log.warning(
            "YouTube client secrets not found at %s; YouTube uploads will fail "
            "until you place the OAuth client_secrets.json there.", path,
        )
