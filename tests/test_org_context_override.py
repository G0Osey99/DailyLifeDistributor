"""Thread-local override for org_context.

Used by worker threads (calendar refresh sources, /upload web worker,
each per-platform upload thread) to honor the org context the request
set up. Without this override, those threads would call
effective_org_id() with no Flask context and read from the legacy
unscoped slot, which after the wipe is empty — refresh / upload
report "session expired" even though the sidebar shows everything
linked.
"""
from __future__ import annotations

import threading

import pytest
from flask import Flask, session as flask_session

from core import org_context


@pytest.fixture()
def app_ctx():
    app = Flask(__name__); app.secret_key = "t"
    with app.test_request_context():
        yield


def test_no_override_uses_session(app_ctx):
    flask_session["current_org_id"] = 5
    assert org_context.effective_org_id() == 5


def test_override_wins_over_session(app_ctx):
    flask_session["current_org_id"] = 5
    with org_context.override(99):
        assert org_context.effective_org_id() == 99
    # Exit restores prior view.
    assert org_context.effective_org_id() == 5


def test_override_none_is_noop(app_ctx):
    flask_session["current_org_id"] = 5
    with org_context.override(None):
        assert org_context.effective_org_id() == 5


def test_override_works_outside_request_context():
    # Bare module call, no Flask context anywhere.
    assert org_context.effective_org_id() is None
    with org_context.override(7):
        assert org_context.effective_org_id() == 7
    assert org_context.effective_org_id() is None


def test_override_is_thread_local():
    """Setting override on one thread must NOT leak into another."""
    seen: dict[str, int | None] = {}

    def child():
        seen["child_initial"] = org_context.effective_org_id()
        with org_context.override(2):
            seen["child_inside"] = org_context.effective_org_id()
        seen["child_after"] = org_context.effective_org_id()

    with org_context.override(1):
        assert org_context.effective_org_id() == 1
        t = threading.Thread(target=child)
        t.start(); t.join()
        # Parent thread still at 1; child saw None / 2 / None.
        assert org_context.effective_org_id() == 1

    assert seen == {"child_initial": None, "child_inside": 2, "child_after": None}


def test_override_restores_nested(app_ctx):
    """Nested overrides restore the outer value, not session."""
    flask_session["current_org_id"] = 5
    with org_context.override(10):
        with org_context.override(20):
            assert org_context.effective_org_id() == 20
        assert org_context.effective_org_id() == 10
    assert org_context.effective_org_id() == 5
