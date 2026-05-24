"""Schema: audit_log + audit_log_archive carry acting_as_org_id."""
from __future__ import annotations

from core import db as _db


def _columns(table: str) -> set[str]:
    with _db._get_conn() as c:
        return {r[1] for r in c.execute(f"PRAGMA table_info('{table}')").fetchall()}


def test_audit_log_has_acting_as_org_id():
    assert "acting_as_org_id" in _columns("audit_log")


def test_audit_log_archive_has_acting_as_org_id():
    assert "acting_as_org_id" in _columns("audit_log_archive")


import pytest
from flask import Flask

from core import org_context


@pytest.fixture()
def app_ctx():
    app = Flask(__name__)
    app.secret_key = "test"
    with app.test_request_context():
        yield


def test_effective_org_id_returns_none_outside_session(app_ctx):
    assert org_context.effective_org_id() is None


def test_effective_org_id_returns_current_when_not_acting(app_ctx):
    from flask import session
    session["current_org_id"] = 3
    assert org_context.effective_org_id() == 3
    assert org_context.is_impersonating() is False
    assert org_context.acting_as_org_id() is None


def test_effective_org_id_returns_acting_when_set(app_ctx):
    from flask import session
    session["current_org_id"] = 3
    session["acting_as_org_id"] = 11
    assert org_context.effective_org_id() == 11
    assert org_context.is_impersonating() is True
    assert org_context.acting_as_org_id() == 11


def test_real_user_id_is_always_session_user_id(app_ctx):
    from flask import session
    session["user_id"] = 7
    session["acting_as_org_id"] = 11
    assert org_context.real_user_id() == 7


def test_effective_org_id_handles_zero_acting_as(app_ctx):
    """Regression: acting_as_org_id=0 must NOT fall through to current_org_id."""
    from flask import session
    session["current_org_id"] = 5
    session["acting_as_org_id"] = 0
    # Even though 0 is falsy, it's an explicit acting-as setting.
    assert org_context.effective_org_id() == 0
    assert org_context.is_impersonating() is True


def test_forbidden_during_impersonation_passes_when_not_impersonating(app_ctx):
    """Decorator is a no-op when acting_as_org_id is unset."""
    from core.org_context import forbidden_during_impersonation

    @forbidden_during_impersonation
    def view():
        return "ok"
    assert view() == "ok"


def test_forbidden_during_impersonation_aborts_when_impersonating(app_ctx):
    """Decorator returns 409 when acting_as_org_id is set."""
    from flask import session
    from werkzeug.exceptions import Conflict
    import pytest as _pytest
    from core.org_context import forbidden_during_impersonation

    session["acting_as_org_id"] = 11

    @forbidden_during_impersonation
    def view():
        return "should-not-run"

    with _pytest.raises(Conflict):
        view()
