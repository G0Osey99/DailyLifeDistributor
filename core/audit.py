"""Audit event writer — persist every privileged action.

`write_event` is the only public API. Callers pass action + actor + org +
optional target + metadata + ip/ua, and we drop the row in `audit_log`.
Reads come through `core.db.list_audit_events`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from core import db as _db


def write_event(
    *,
    action: str,
    actor_user_id: int | None = None,
    org_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    ip: str | None = None,
    ua: str | None = None,
    acting_as_org_id: int | None = None,
) -> int:
    """Persist an audit event and return its row id.

    When ``acting_as_org_id`` is not supplied, falls back to the
    flask session's ``acting_as_org_id`` value if a request context
    is active. Callers in non-request code paths can pass it
    explicitly (or leave it None for non-impersonated actions).
    """
    now = datetime.now(timezone.utc).isoformat()
    meta_json = json.dumps(metadata, default=str) if metadata is not None else None
    if acting_as_org_id is None:
        try:
            from flask import has_request_context, session
            if has_request_context():
                v = session.get("acting_as_org_id")
                if v is not None:
                    acting_as_org_id = int(v)
        except Exception:
            pass
    return _db.insert_audit_event(
        org_id=org_id,
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        metadata=meta_json,
        ip=ip,
        user_agent=ua,
        created_at=now,
        acting_as_org_id=acting_as_org_id,
    )
