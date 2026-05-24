"""audit.write_event auto-fills acting_as_org_id from session."""
from __future__ import annotations

import pytest
from flask import Flask

from core import audit, db


@pytest.fixture()
def app_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    import importlib
    importlib.reload(db); db.init_db()
    app = Flask(__name__); app.secret_key = "test"
    with app.test_request_context():
        yield


def _last_event() -> dict:
    with db._get_conn() as c:
        row = c.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else {}


def test_write_event_records_acting_as_org_id_from_session(app_ctx):
    from flask import session
    session["acting_as_org_id"] = 42
    audit.write_event(action="test", actor_user_id=1, org_id=42)
    assert _last_event()["acting_as_org_id"] == 42


def test_write_event_records_null_when_not_impersonating(app_ctx):
    audit.write_event(action="test", actor_user_id=1, org_id=1)
    assert _last_event()["acting_as_org_id"] is None


def test_write_event_explicit_override_wins(app_ctx):
    from flask import session
    session["acting_as_org_id"] = 5
    audit.write_event(
        action="test", actor_user_id=1, org_id=5, acting_as_org_id=99,
    )
    assert _last_event()["acting_as_org_id"] == 99
