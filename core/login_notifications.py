"""Email the user on first sighting of (user_id, ip).

A second sighting of the same pair updates `last_seen` but does NOT send a
new email — that's the difference between "I'm signing in from my couch
again" and "someone in another country has my password".
"""
from __future__ import annotations

from datetime import datetime, timezone

from flask import current_app, url_for

from core import db as _db
from core import email as _email


def notify_if_new_device(user_id: int, ip: str, ua: str) -> None:
    """If the (user, ip) pair is brand-new, send a heads-up email.

    Respects the per-user `notify_new_device` preference (column added in
    the same phase γ migration). Never raises — a failed email is a
    log-only event, not a login-blocker.
    """
    user = _db.get_user_by_id(user_id)
    if not user:
        return
    if not user.get("notify_new_device", 1):
        return
    now = datetime.now(timezone.utc)
    is_new = _db.upsert_login_ip_sighting(user_id, ip, now.isoformat())
    if not is_new:
        return
    try:
        base = current_app.config.get("BASE_URL", "https://autoalert.pro")
    except RuntimeError:
        # Outside an app context (unit test); fall back to a default.
        base = "https://autoalert.pro"
    try:
        login_url = base + url_for("auth.login")
    except Exception:
        login_url = base + "/login"
    _email.send(
        "login_new_device",
        to=user["email"],
        when=now.isoformat(),
        ip=ip,
        ua=ua,
        reset_url=login_url,
        settings_url=base + "/settings/security",
    )
