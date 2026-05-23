"""Tests for blueprints.agent._client_ip — proxy-aware client IP resolution."""
from __future__ import annotations

import pytest
from flask import Flask


@pytest.fixture
def app():
    return Flask(__name__)


def _call(app, headers=None, remote="9.9.9.9"):
    """Invoke _client_ip inside a Flask test_request_context with the given
    headers + REMOTE_ADDR. Returns the resolved string."""
    from blueprints.agent import _client_ip
    h = headers or {}
    with app.test_request_context(headers=h, environ_overrides={
        "REMOTE_ADDR": remote,
    }):
        return _client_ip()


def test_cf_connecting_ip_wins(app):
    """CF-Connecting-IP is trusted first when present (Cloudflare deploys)."""
    ip = _call(app, headers={
        "CF-Connecting-IP": "1.2.3.4",
        "X-Forwarded-For": "5.6.7.8",
    })
    assert ip == "1.2.3.4"


def test_xff_used_when_cf_missing(app):
    """Without CF-Connecting-IP, the first X-Forwarded-For entry wins."""
    ip = _call(app, headers={"X-Forwarded-For": "5.6.7.8"})
    assert ip == "5.6.7.8"


def test_xff_first_entry_used_when_chain(app):
    """X-Forwarded-For chains: only the first (real client) entry is used."""
    ip = _call(app, headers={"X-Forwarded-For": "5.6.7.8, 10.0.0.1, 10.0.0.2"})
    assert ip == "5.6.7.8"


def test_remote_addr_fallback(app):
    """No proxy headers → request.remote_addr."""
    ip = _call(app, remote="9.9.9.9")
    assert ip == "9.9.9.9"


def test_unknown_when_nothing_available(app):
    """All inputs missing → 'unknown' (rather than None/empty string)."""
    # Flask sets REMOTE_ADDR from werkzeug's default test env; force-blank it.
    ip = _call(app, remote="")
    assert ip == "unknown"


def test_empty_cf_falls_through_to_xff(app):
    """An empty CF-Connecting-IP header doesn't override XFF."""
    ip = _call(app, headers={
        "CF-Connecting-IP": "   ",
        "X-Forwarded-For": "5.6.7.8",
    })
    assert ip == "5.6.7.8"


def test_empty_xff_falls_through_to_remote(app):
    """An empty X-Forwarded-For (or whitespace) doesn't override remote_addr."""
    ip = _call(app, headers={"X-Forwarded-For": "   "}, remote="9.9.9.9")
    assert ip == "9.9.9.9"
