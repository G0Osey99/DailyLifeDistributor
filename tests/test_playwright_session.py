"""Tests for core.playwright_session helpers.

Covers the parts that don't actually require Playwright:

- url_marker_login_check matching semantics
- SessionConfig defaulting (login_url falls back to target_url)
- Login-timeout env var resolution
- Headless preference resolution
"""
from __future__ import annotations


import pytest

from core.playwright_session import (
    PlaywrightSession,
    SessionConfig,
    url_marker_login_check,
)


def test_url_marker_login_check_matches_any_substring():
    is_login = url_marker_login_check(("/login", "/oauth"))
    assert is_login("https://example.com/login") is True
    assert is_login("https://example.com/oauth/callback") is True
    assert is_login("https://example.com/dashboard") is False


def test_url_marker_login_check_is_case_insensitive():
    is_login = url_marker_login_check(("/Login",))
    assert is_login("https://EXAMPLE.com/login") is True
    assert is_login("https://example.com/LOGIN/foo") is True


def test_url_marker_login_check_handles_empty_url():
    is_login = url_marker_login_check(("/login",))
    assert is_login("") is False
    assert is_login(None) is False  # type: ignore[arg-type]


def test_session_config_login_url_defaults_to_target_url():
    cfg = SessionConfig(
        name="x",
        session_file="/tmp/x.json",
        is_login_url=lambda u: False,
        target_url="https://example.com/dashboard",
    )
    assert cfg.login_url == "https://example.com/dashboard"


def test_session_config_explicit_login_url_wins():
    cfg = SessionConfig(
        name="x",
        session_file="/tmp/x.json",
        is_login_url=lambda u: False,
        target_url="https://example.com/dashboard",
        login_url="https://example.com/sign-in",
    )
    assert cfg.login_url == "https://example.com/sign-in"


def _bare_session(cfg: SessionConfig) -> PlaywrightSession:
    """Build a PlaywrightSession without entering it (no Playwright launch)."""
    # Bypass __init__'s sync_playwright check — we only exercise pure helpers.
    obj = PlaywrightSession.__new__(PlaywrightSession)
    obj.config = cfg
    obj._progress = None
    obj._pw = None
    obj.browser = None
    obj.context = None
    obj.page = None
    return obj


def test_login_timeout_falls_back_to_default(monkeypatch):
    cfg = SessionConfig(
        name="x", session_file="/tmp/x.json", is_login_url=lambda u: False,
        login_timeout_env="DLD_TEST_LOGIN_TIMEOUT", default_login_timeout=42,
    )
    monkeypatch.delenv("DLD_TEST_LOGIN_TIMEOUT", raising=False)
    sess = _bare_session(cfg)
    assert sess._login_timeout_seconds() == 42


def test_login_timeout_reads_env(monkeypatch):
    cfg = SessionConfig(
        name="x", session_file="/tmp/x.json", is_login_url=lambda u: False,
        login_timeout_env="DLD_TEST_LOGIN_TIMEOUT", default_login_timeout=42,
    )
    monkeypatch.setenv("DLD_TEST_LOGIN_TIMEOUT", "120")
    sess = _bare_session(cfg)
    assert sess._login_timeout_seconds() == 120


def test_login_timeout_ignores_non_integer_env(monkeypatch):
    cfg = SessionConfig(
        name="x", session_file="/tmp/x.json", is_login_url=lambda u: False,
        login_timeout_env="DLD_TEST_LOGIN_TIMEOUT", default_login_timeout=42,
    )
    monkeypatch.setenv("DLD_TEST_LOGIN_TIMEOUT", "not-a-number")
    sess = _bare_session(cfg)
    assert sess._login_timeout_seconds() == 42


def test_headless_pref_default_false(monkeypatch):
    cfg = SessionConfig(
        name="x", session_file="/tmp/x.json", is_login_url=lambda u: False,
        headless_env="DLD_TEST_HEADLESS",
    )
    monkeypatch.delenv("DLD_TEST_HEADLESS", raising=False)
    sess = _bare_session(cfg)
    assert sess._headless_pref() is False


@pytest.mark.parametrize("value,expected", [
    ("true", True),
    ("True", True),
    ("TRUE", True),
    ("false", False),
    ("0", False),
    ("", False),
    ("yes", False),  # only "true" is honored
])
def test_headless_pref_parses_env(monkeypatch, value, expected):
    cfg = SessionConfig(
        name="x", session_file="/tmp/x.json", is_login_url=lambda u: False,
        headless_env="DLD_TEST_HEADLESS",
    )
    monkeypatch.setenv("DLD_TEST_HEADLESS", value)
    sess = _bare_session(cfg)
    assert sess._headless_pref() is expected


def test_headless_pref_without_env_var_attribute():
    """No headless_env configured => always headed."""
    cfg = SessionConfig(
        name="x", session_file="/tmp/x.json", is_login_url=lambda u: False,
    )
    sess = _bare_session(cfg)
    assert sess._headless_pref() is False
