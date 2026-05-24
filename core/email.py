"""Resend transactional email wrapper.

Falls back to a WARNING-logged no-op when RESEND_API_KEY is unset so dev
runs and the bootstrap migration don't require an API key.

Resilience: every send goes through the ``email:resend`` circuit breaker
and one transient retry on connection error / 5xx. A 30-second Resend
outage during a recovery flow (~5 emails) used to block the request on
back-to-back SDK timeouts; the breaker opens after 3 consecutive
failures and fails-fast for the next 60s so the request doesn't hang.
"""
from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path
from typing import Tuple

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core import circuit_breaker as _cb

try:
    import resend  # type: ignore
except ImportError:  # pragma: no cover - resend is a required pin
    resend = None  # noqa: F841

log = logging.getLogger(__name__)

# Breaker tuning: a single failed send is usually a transient hiccup;
# three in a row almost certainly means Resend is down or our domain
# verification flipped. 60s cooldown is short enough that a healthy
# Resend recovers within the same /recover/<id>/approve request and
# long enough that we don't hammer a down provider.
_RESEND_BREAKER = _cb.get_breaker(
    "email:resend", failure_threshold=3, recovery_timeout=60.0,
)
_TRANSIENT_RETRY_DELAY = 0.5  # seconds; jittered ±50%


class UnknownTemplateError(LookupError):
    """Raised when render_template() is called with a missing template name."""


_TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent / "templates" / "email"
)
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
    keep_trailing_newline=True,
)

# Subject lines per template (one source of truth so render+send agree).
_SUBJECTS = {
    "welcome": "Welcome to {org_name} on Daily Life Distributor",
    "invite": "You've been invited to {org_name} on Daily Life Distributor",
    # Phase γ: 2FA, recovery, password reset, new-device notifications.
    "2fa_code": "Your Daily Life Distributor login code",
    "recovery_request": "A recovery request needs your approval",
    "recovery_approved": "Your recovery request was approved",
    "password_reset": "Reset your Daily Life Distributor password",
    "login_new_device": "New sign-in to your Daily Life Distributor account",
}

_FROM_ADDR = os.environ.get(
    "RESEND_FROM_ADDR", "Daily Life Distributor <noreply@autoalert.pro>",
)


def render_template(name: str, **vars) -> Tuple[str, str, str]:
    """Return (subject, html, text) for the named template.

    Raises UnknownTemplateError if either the .html or .txt is missing
    or the subject is undeclared.
    """
    if name not in _SUBJECTS:
        raise UnknownTemplateError(f"unknown email template: {name!r}")
    try:
        html = _env.get_template(f"{name}.html").render(**vars)
        text = _env.get_template(f"{name}.txt").render(**vars)
    except Exception as e:
        raise UnknownTemplateError(f"failed to render {name}: {e}") from e
    subject = _SUBJECTS[name].format(**vars)
    return subject, html, text


def send(name: str, to: str, **vars) -> bool:
    """Send a transactional email via Resend.

    Returns True on success, False on no-op (missing API key) or send
    failure. No-op + WARNING when RESEND_API_KEY is unset so the dev path
    and the bootstrap migration both keep working.
    """
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        log.warning(
            "RESEND_API_KEY not set — skipping email %r to %s (template=%s)",
            name, to, name,
        )
        return False
    if resend is None:
        log.error("resend library not importable; skipping email")
        return False

    try:
        subject, html, text = render_template(name, **vars)
    except UnknownTemplateError:
        log.exception("Email template render failed; skipping send")
        return False

    # Breaker first — if Resend has been failing, fail fast.
    if not _RESEND_BREAKER.allow():
        log.warning(
            "email: circuit '%s' is OPEN; skipping send template=%s to=%s",
            _RESEND_BREAKER.name, name, to,
        )
        return False

    resend.api_key = api_key
    payload = {
        "from": _FROM_ADDR,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
    }

    # One transient retry on connection error / 5xx. The SDK doesn't
    # expose the response object cleanly, so we treat any exception
    # other than what looks like a validation error (4xx-shaped
    # messages) as retryable. A second consecutive failure trips
    # record_failure once — not twice — to avoid skewing the breaker.
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            resend.Emails.send(payload)
            _RESEND_BREAKER.record_success()
            return True
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            # Don't retry obvious client-side validation errors —
            # those will fail the same way every time.
            if any(s in msg for s in (
                "invalid", "validation", "unauthorized", "forbidden",
                "bad request", "domain not verified",
            )):
                break
            if attempt == 1:
                # Jittered short sleep before the second attempt.
                time.sleep(_TRANSIENT_RETRY_DELAY * (0.5 + random.random()))
                continue

    _RESEND_BREAKER.record_failure()
    log.exception(
        "Resend send failed for template=%s to=%s (last error: %s)",
        name, to, last_exc,
    )
    return False
