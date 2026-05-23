"""Resend transactional email wrapper.

Wired but unused: no live emails are sent in phase α (call sites land in
PR-β invites and PR-γ recovery). render_template() is exercised by tests
so we know the templates parse and the variable contract is stable.

Falls back to a WARNING-logged no-op when RESEND_API_KEY is unset so dev
runs and the bootstrap migration don't require an API key.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Tuple

from jinja2 import Environment, FileSystemLoader, select_autoescape

try:
    import resend  # type: ignore
except ImportError:  # pragma: no cover - resend is a required pin
    resend = None  # noqa: F841

log = logging.getLogger(__name__)


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

    try:
        resend.api_key = api_key
        resend.Emails.send({
            "from": _FROM_ADDR,
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        })
        return True
    except Exception:
        log.exception("Resend send failed for template=%s to=%s", name, to)
        return False
