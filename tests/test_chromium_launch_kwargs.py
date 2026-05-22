"""chromium_launch_kwargs picks executable_path vs channel correctly.

This is the logic the calendar-refresh sources need so they launch the VPS's
chromium (via *_CHROME_PATH) instead of a missing Google Chrome.
"""
from core.playwright_session import chromium_launch_kwargs


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
