"""The /health/alerts uptime-monitor endpoint.

Unlike /health/details (always 200, payload is the signal), /health/alerts
returns 503 when something needs operator attention. That's the contract
external services like Uptime Robot can watch on HTTP status code alone.
"""
from __future__ import annotations

import pytest

from core import circuit_breaker as _cb


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "false")
    monkeypatch.setenv("RATELIMIT_ENABLED", "false")
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-health-alerts")
    monkeypatch.setenv("HOSTED", "")
    from app import create_app
    a = create_app()
    a.config["TESTING"] = True
    yield a


@pytest.fixture
def client(app):
    return app.test_client()


def _reset_breakers():
    """Each test starts from a clean registry so prior state doesn't bleed."""
    _cb.reset_all()


def test_healthy_returns_200_with_empty_alerts(client):
    _reset_breakers()
    r = client.get("/health/alerts")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["ok"] is True
    assert payload["alerts"] == []
    assert payload["critical_count"] == 0
    assert payload["alert_count"] == 0


def test_open_breaker_returns_503_with_named_alert(client):
    _reset_breakers()
    # Trip a breaker by recording 3 consecutive failures (default threshold).
    b = _cb.get_breaker("test:fake-provider")
    for _ in range(3):
        b.record_failure()
    assert b.state.value == "open"

    r = client.get("/health/alerts")
    assert r.status_code == 503
    payload = r.get_json()
    assert payload["ok"] is False
    assert payload["alert_count"] >= 1
    codes = [a["code"] for a in payload["alerts"]]
    assert "breaker_open:test:fake-provider" in codes
    # Breaker-open is a warning, not critical.
    breaker_alert = next(
        a for a in payload["alerts"]
        if a["code"] == "breaker_open:test:fake-provider"
    )
    assert breaker_alert["severity"] == "warning"
    _reset_breakers()  # don't leak into other tests


def test_missing_secret_enc_key_is_critical_alert(client, monkeypatch):
    _reset_breakers()
    monkeypatch.delenv("SECRET_ENC_KEY", raising=False)
    r = client.get("/health/alerts")
    assert r.status_code == 503
    payload = r.get_json()
    codes = [a["code"] for a in payload["alerts"]]
    assert "secret_enc_key_missing" in codes
    assert payload["critical_count"] >= 1


def test_alerts_payload_shape_is_stable(client):
    """Schema contract — external monitors will scrape these keys."""
    _reset_breakers()
    r = client.get("/health/alerts")
    payload = r.get_json()
    # Required top-level keys.
    for key in ("ok", "critical_count", "alert_count", "alerts"):
        assert key in payload, f"missing top-level key {key!r}"
    # Each alert entry has the documented shape.
    for alert in payload["alerts"]:
        assert "severity" in alert
        assert "code" in alert
        assert "message" in alert
        assert alert["severity"] in ("critical", "warning")
