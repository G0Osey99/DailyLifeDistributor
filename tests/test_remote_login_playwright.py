"""Browser-free checks for the Playwright handle wiring.

We don't launch Chrome in CI; we only assert the module imports and that a
launch failure surfaces as an exception (by pointing channel at a bogus binary
path env that the handle will try and fail to use)."""
import pytest

pytest.importorskip("playwright")

from core import remote_login_playwright as rlp
from core.playwright_session import SessionConfig


def test_launcher_callable_exists():
    assert callable(rlp.default_browser_launcher)


def test_launch_failure_propagates(monkeypatch, tmp_path):
    # Force a launch failure: a chrome path env pointing at a nonexistent file.
    monkeypatch.setenv("RL_TEST_CHROME", str(tmp_path / "nope"))
    cfg = SessionConfig(
        name="x",
        session_file=str(tmp_path / "x_session.json"),
        is_login_url=lambda u: False,
        chrome_path_env="RL_TEST_CHROME",
    )
    with pytest.raises(Exception):
        rlp.default_browser_launcher(cfg)
