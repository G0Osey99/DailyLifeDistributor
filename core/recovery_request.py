"""Admin-approved out-of-band recovery requests.

A user who has lost their password AND their second factor AND their
backup codes can submit one of these. Owners of any org they belong to
get an email with an approval link; once they approve, a one-time
password-reset link emails to the requester.

Rate-limit: one open request per user per 24 hours. Unknown usernames
return silently (no enumeration).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from flask import current_app, url_for

from core import audit as _audit
from core import db as _db
from core import email as _email

log = logging.getLogger(__name__)

_REQ_TTL = timedelta(hours=48)
_RATE_WINDOW = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def submit_request(username: str, note: str) -> int | None:
    """Insert a recovery_requests row and email every Owner of the user's orgs.

    Returns the new row id, or None when:
      - the username is unknown (no enumeration), OR
      - the user already has a recovery_requests row in the past 24h.
    """
    user = _db.get_user_by_username(username)
    if not user:
        return None
    recent_iso = (_now() - _RATE_WINDOW).isoformat()
    if _db.count_recovery_requests_since(user["id"], recent_iso) > 0:
        return None
    now = _now()
    rid = _db.insert_recovery_request(
        user_id=user["id"],
        requested_at=now.isoformat(),
        expires_at=(now + _REQ_TTL).isoformat(),
        note=note,
    )
    # Emails go to every Owner across every org the user belongs to.
    try:
        base = current_app.config.get("BASE_URL", "https://autoalert.pro")
    except RuntimeError:
        base = "https://autoalert.pro"
    try:
        approve_path = url_for("recovery.approve", request_id=rid)
    except Exception:
        approve_path = f"/admin-actions/recovery/{rid}/approve"
    approve_url = base + approve_path
    owners = _db.list_org_owners_for_user(user["id"])
    for o in owners:
        try:
            _email.send(
                "recovery_request",
                to=o["email"],
                requester_username=user["username"],
                requester_email=user["email"],
                note=note,
                approve_url=approve_url,
            )
        except Exception:
            # Non-fatal: keep iterating so other Owners still get
            # notified. But log — silent failure of a security email
            # means a real recovery request could go unanswered.
            log.warning(
                "recovery_request email to owner=%s for requester=%s failed",
                o.get("email"), user.get("email"), exc_info=True,
            )
    _audit.write_event(
        action="user.recovery_requested",
        actor_user_id=user["id"],
        target_type="user", target_id=user["id"],
        metadata={"note": (note or "")[:200]},
    )
    return rid
