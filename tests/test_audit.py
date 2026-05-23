"""Phase γ Task 19: audit.write_event persistence."""
from __future__ import annotations

import json

from core import audit


def test_write_event_persists_row(db):
    audit.write_event(
        action="user.login", actor_user_id=1, org_id=2,
        target_type="user", target_id=1,
        metadata={"k": "v"}, ip="1.2.3.4", ua="Mozilla",
    )
    rows = db.list_audit_events(org_id=2)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "user.login"
    assert r["actor_user_id"] == 1
    assert r["org_id"] == 2
    assert r["target_type"] == "user"
    assert r["target_id"] == 1
    assert json.loads(r["metadata"]) == {"k": "v"}
    assert r["ip"] == "1.2.3.4"
    assert r["user_agent"] == "Mozilla"


def test_write_event_handles_nulls(db):
    audit.write_event(action="system.boot")
    rows = db.list_audit_events()
    assert len(rows) == 1
    assert rows[0]["action"] == "system.boot"
    assert rows[0]["actor_user_id"] is None
    assert rows[0]["metadata"] is None


def test_list_audit_events_filters_by_action_prefix(db):
    audit.write_event(action="user.login", org_id=1)
    audit.write_event(action="upload.started", org_id=1)
    rows = db.list_audit_events(org_id=1, action_prefix="upload.")
    assert len(rows) == 1
    assert rows[0]["action"] == "upload.started"


def test_list_audit_events_filters_by_actor(db):
    audit.write_event(action="user.login", actor_user_id=1, org_id=1)
    audit.write_event(action="user.login", actor_user_id=2, org_id=1)
    rows = db.list_audit_events(org_id=1, actor_user_id=2)
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == 2
