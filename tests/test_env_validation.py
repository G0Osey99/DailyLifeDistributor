"""Startup environment-variable validation."""
import pytest

from core import env_validation as ev


def test_numeric_keys_accept_ints(monkeypatch):
    monkeypatch.setenv("SIMPLECAST_LOGIN_TIMEOUT", "300")
    monkeypatch.setenv("VISTA_SOCIAL_LOGIN_TIMEOUT", "120")
    monkeypatch.setenv("MAX_CONTENT_LENGTH_BYTES", "115343360")
    ev._check_numeric(ev._NUMERIC_KEYS)  # no raise


def test_numeric_keys_reject_garbage(monkeypatch):
    monkeypatch.setenv("VISTA_SOCIAL_LOGIN_TIMEOUT", "soon")
    with pytest.raises(RuntimeError) as exc:
        ev._check_numeric(ev._NUMERIC_KEYS)
    assert "VISTA_SOCIAL_LOGIN_TIMEOUT" in str(exc.value)


def test_unset_numeric_keys_are_skipped(monkeypatch):
    for k in ev._NUMERIC_KEYS:
        monkeypatch.delenv(k, raising=False)
    ev._check_numeric(ev._NUMERIC_KEYS)  # no raise


def test_hosted_requires_flask_secret_key(monkeypatch):
    monkeypatch.setenv("HOSTED", "true")
    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc:
        ev._check_hosted_requirements()
    assert "FLASK_SECRET_KEY" in str(exc.value)


def test_hosted_passes_with_secret(monkeypatch):
    monkeypatch.setenv("HOSTED", "true")
    monkeypatch.setenv("FLASK_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("ALLOWED_HOSTS", "autoalert.pro")
    ev._check_hosted_requirements()  # no raise


def test_non_hosted_does_not_require_secret(monkeypatch):
    monkeypatch.delenv("HOSTED", raising=False)
    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
    ev._check_hosted_requirements()  # no raise


def test_validate_env_propagates_bad_numeric(monkeypatch):
    monkeypatch.delenv("HOSTED", raising=False)
    monkeypatch.setenv("ROCK_LOGIN_TIMEOUT", "nope")
    with pytest.raises(RuntimeError):
        ev.validate_env()
