"""Regression tests for /sessions/status — the sidebar status feed.

Originally written because the route did
    `from core.relay import RELAY, _ACCOUNT`
inside a try/except — but `core.relay` doesn't export those names (the
real RELAY lives on blueprints.agent). The ImportError was silently
swallowed and the sidebar always reported 'No agent connected' even
when an agent was registered on the relay.
"""
from __future__ import annotations

import pytest

from core import relay as _relay


@pytest.fixture
def app(monkeypatch):
    """Local override of the conftest app fixture — re-enables legacy
    auth so we can fake-login via session['authenticated']=True without
    seeding a user/membership."""
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "true")
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "false")
    monkeypatch.setenv("RATELIMIT_ENABLED", "false")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-key")
    from app import create_app
    a = create_app()
    a.config["TESTING"] = True
    yield a


def _restore_default_relay():
    """Best-effort cleanup so a test's fake relay doesn't bleed into the next."""
    _relay.set_default_relay(None, account="default")  # type: ignore[arg-type]


def test_sessions_status_returns_agent_key(app):
    """The 'agent' row must always be present in the payload."""
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["authenticated"] = True
        r = c.get("/sessions/status")
    assert r.status_code == 200
    payload = r.get_json()
    assert "agent" in payload
    assert "ok" in payload["agent"]


def test_sessions_status_reports_agent_ok_when_registered(app):
    """When an agent is registered on the default relay, ok must be True.

    Reproduces the bug where the route silently returned ok=False because
    it tried to import a symbol that doesn't exist (core.relay.RELAY) and
    the except-Exception around the import swallowed the ImportError.
    """
    test_relay = _relay.Relay()
    test_relay.register_agent("default", "dev-test",
                               lambda _m: None, device_name="laptop")
    _relay.set_default_relay(test_relay, account="default")
    try:
        with app.test_client() as c:
            with c.session_transaction() as s:
                s["authenticated"] = True
            r = c.get("/sessions/status")
        assert r.status_code == 200
        payload = r.get_json()
        assert payload["agent"]["ok"] is True, payload
        assert payload["agent"]["label_on"] == "Agent online"
    finally:
        _restore_default_relay()


def test_sessions_status_reports_count_when_multiple(app):
    """Label switches to 'Agent online (N)' when N > 1."""
    test_relay = _relay.Relay()
    test_relay.register_agent("default", "dev-a", lambda _m: None, device_name="a")
    test_relay.register_agent("default", "dev-b", lambda _m: None, device_name="b")
    _relay.set_default_relay(test_relay, account="default")
    try:
        with app.test_client() as c:
            with c.session_transaction() as s:
                s["authenticated"] = True
            r = c.get("/sessions/status")
        payload = r.get_json()
        assert payload["agent"]["ok"] is True
        assert payload["agent"]["label_on"] == "Agent online (2)"
    finally:
        _restore_default_relay()
