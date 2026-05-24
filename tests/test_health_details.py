"""The /health/details diagnostic endpoint.

This endpoint always returns 200 (it's introspection, not a probe); the
payload shape is the contract external monitors scrape.
"""
from __future__ import annotations

import pytest

from core import circuit_breaker as _cb


@pytest.fixture
def app(monkeypatch):
    """Local app fixture that allows the legacy session shape we'll mint.

    The default conftest app sets LEGACY_PASSWORD_ENABLED=false, which is
    fine — /health/details is in the public-endpoint list.
    """
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "false")
    monkeypatch.setenv("RATELIMIT_ENABLED", "false")
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-health-details")
    monkeypatch.setenv("HOSTED", "")
    from app import create_app
    a = create_app()
    a.config["TESTING"] = True
    yield a


@pytest.fixture
def client(app):
    return app.test_client()


def test_health_details_returns_200_always(client):
    """Diagnostic endpoint must never 503 — interpretation is the caller's job."""
    r = client.get("/health/details")
    assert r.status_code == 200


def test_health_details_payload_has_required_keys(client):
    r = client.get("/health/details")
    body = r.get_json()
    for key in (
        "breakers",
        "agents_online",
        "resend_configured",
        "youtube_quota",
        "secret_enc_key_set",
    ):
        assert key in body, f"missing top-level key {key!r}"


def test_health_details_reflects_tripped_breaker(client):
    """A breaker tripped via the registry must surface as state='open'."""
    br = _cb.get_breaker("test:fake", failure_threshold=3, recovery_timeout=120.0)
    for _ in range(3):
        br.record_failure()
    assert br.state == _cb.CircuitState.OPEN

    r = client.get("/health/details")
    body = r.get_json()
    assert "test:fake" in body["breakers"], body["breakers"]
    assert body["breakers"]["test:fake"]["state"] == "open"


def test_health_details_resend_configured_flips_with_env(client, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    body = client.get("/health/details").get_json()
    assert body["resend_configured"] is False

    monkeypatch.setenv("RESEND_API_KEY", "re_test_xxx")
    body = client.get("/health/details").get_json()
    assert body["resend_configured"] is True


def test_health_details_youtube_quota_shape(client):
    body = client.get("/health/details").get_json()
    yq = body["youtube_quota"]
    # Either the normal payload OR an error dict — both shapes are
    # documented in /health/details. Real path should hit the normal one.
    if "error" in yq:
        return
    assert set(yq.keys()) >= {"used", "cap", "pct"}
    assert isinstance(yq["used"], int)
    assert isinstance(yq["cap"], int)


def test_health_details_secret_enc_key_set_true_when_present(client):
    # The autouse _master_key fixture always sets it.
    body = client.get("/health/details").get_json()
    assert body["secret_enc_key_set"] is True
