"""Unit tests for the shared-credential auth core."""
import pytest

from core import auth


@pytest.fixture(autouse=True)
def _db(temp_db):
    auth.reset_lockouts()
    yield


def test_not_configured_initially():
    assert auth.is_configured() is False


def test_set_and_verify_password():
    auth.set_password("hunter2")
    assert auth.is_configured() is True
    assert auth.verify_password("hunter2") is True
    assert auth.verify_password("wrong") is False


def test_hash_not_stored_plaintext():
    auth.set_password("plaintextpw")
    from core import secrets_store
    stored = secrets_store.get_secret(auth._HASH_SECRET)
    assert stored is not None
    assert "plaintextpw" not in stored


def test_bootstrap_from_env_seeds_password(monkeypatch):
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "seeded-pw")
    auth.bootstrap_from_env()
    assert auth.verify_password("seeded-pw") is True


def test_bootstrap_does_not_overwrite_existing(monkeypatch):
    auth.set_password("original")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "should-be-ignored")
    auth.bootstrap_from_env()
    assert auth.verify_password("original") is True


def test_lockout_after_threshold():
    auth.set_password("pw")
    ip = "10.0.0.5"
    for _ in range(auth.MAX_ATTEMPTS):
        assert auth.is_locked(ip) is False
        auth.record_failure(ip)
    assert auth.is_locked(ip) is True


def test_success_clears_failures():
    ip = "10.0.0.6"
    auth.record_failure(ip)
    auth.clear_failures(ip)
    assert auth.is_locked(ip) is False
