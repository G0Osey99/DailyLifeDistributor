"""chromium_launch_kwargs picks executable_path vs channel correctly.

This is the logic the calendar-refresh sources need so they launch the VPS's
chromium (via *_CHROME_PATH) instead of a missing Google Chrome.
"""
import pytest

from core.playwright_session import chromium_launch_kwargs


@pytest.fixture(autouse=True)
def _clear_bundled_flag(monkeypatch):
    # Default every case to "bundled off" so the channel-fallback tests are
    # order-independent; the bundled-specific tests opt in explicitly. (A
    # leaked DLD_USE_BUNDLED_CHROMIUM from another module's tests otherwise
    # flips these assertions.)
    monkeypatch.delenv("DLD_USE_BUNDLED_CHROMIUM", raising=False)
    yield


def test_uses_executable_path_when_env_set(monkeypatch):
    monkeypatch.setenv("ROCK_CHROME_PATH", "/usr/bin/chromium")
    kw = chromium_launch_kwargs("ROCK_CHROME_PATH", headless=True)
    assert kw["executable_path"] == "/usr/bin/chromium"
    assert "channel" not in kw
    assert kw["headless"] is True


def test_falls_back_to_channel_when_env_unset(monkeypatch):
    monkeypatch.delenv("ROCK_CHROME_PATH", raising=False)
    kw = chromium_launch_kwargs("ROCK_CHROME_PATH", headless=False)
    assert kw["channel"] == "chrome"
    assert "executable_path" not in kw
    assert kw["headless"] is False


def test_blank_env_value_falls_back_to_channel(monkeypatch):
    monkeypatch.setenv("ROCK_CHROME_PATH", "   ")
    kw = chromium_launch_kwargs("ROCK_CHROME_PATH", headless=True)
    assert kw["channel"] == "chrome"
    assert "executable_path" not in kw


def test_no_env_name_uses_channel():
    kw = chromium_launch_kwargs("", headless=True)
    assert kw["channel"] == "chrome"


def test_bundled_chromium_uses_neither_channel_nor_exec(monkeypatch):
    # The agent sets DLD_USE_BUNDLED_CHROMIUM after downloading Playwright's
    # own Chromium; the launch must then carry no channel (no system Chrome)
    # and no executable_path — Playwright resolves the browser from
    # PLAYWRIGHT_BROWSERS_PATH on its own.
    monkeypatch.delenv("ROCK_CHROME_PATH", raising=False)
    monkeypatch.setenv("DLD_USE_BUNDLED_CHROMIUM", "1")
    kw = chromium_launch_kwargs("ROCK_CHROME_PATH", headless=True)
    assert "channel" not in kw
    assert "executable_path" not in kw
    assert kw["headless"] is True


def test_explicit_chrome_path_wins_over_bundled(monkeypatch):
    # The hosted VPS pins ROCK_CHROME_PATH=/usr/bin/chromium. Even if the
    # bundled flag is somehow set, an explicit per-service path must win so
    # the server's behaviour is never altered by this agent-only feature.
    monkeypatch.setenv("DLD_USE_BUNDLED_CHROMIUM", "1")
    monkeypatch.setenv("ROCK_CHROME_PATH", "/usr/bin/chromium")
    kw = chromium_launch_kwargs("ROCK_CHROME_PATH", headless=True)
    assert kw["executable_path"] == "/usr/bin/chromium"
    assert "channel" not in kw


def test_bundled_flag_falsey_still_uses_channel(monkeypatch):
    monkeypatch.delenv("ROCK_CHROME_PATH", raising=False)
    monkeypatch.setenv("DLD_USE_BUNDLED_CHROMIUM", "0")
    kw = chromium_launch_kwargs("ROCK_CHROME_PATH", headless=True)
    assert kw["channel"] == "chrome"
