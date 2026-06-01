"""Tests for agent/chromium.py — bundled-Chromium ensure/env logic.

No real download happens: _install is monkeypatched. We assert the env-var
contract (PLAYWRIGHT_BROWSERS_PATH + DLD_USE_BUNDLED_CHROMIUM) and the
graceful-degrade behaviour when the install fails.
"""
from __future__ import annotations

import os

import pytest

from agent import chromium


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    # Isolate HOME so browsers_dir() lands in a temp tree, and clear the flags
    # so one test's success doesn't leak into another's assertions.
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
    monkeypatch.delenv("DLD_USE_BUNDLED_CHROMIUM", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    yield
    # ensure_chromium()/prepare_env() write os.environ directly (not via
    # monkeypatch), so undo them here or DLD_USE_BUNDLED_CHROMIUM leaks into
    # other modules' tests (e.g. tests/test_chromium_launch_kwargs.py).
    os.environ.pop("DLD_USE_BUNDLED_CHROMIUM", None)
    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


def _make_chromium(bdir: str) -> None:
    os.makedirs(os.path.join(bdir, "chromium-1208"), exist_ok=True)


def test_browsers_dir_under_dld_agent(tmp_path):
    d = chromium.browsers_dir()
    assert d == os.path.join(str(tmp_path), ".dld-agent", "browsers")
    assert os.path.isdir(d)


def test_is_installed_matches_full_chromium_only():
    bdir = chromium.browsers_dir()
    assert chromium.is_installed(bdir) is False
    # The headless-shell dir alone must NOT count as installed.
    os.makedirs(os.path.join(bdir, "chromium_headless_shell-1208"), exist_ok=True)
    assert chromium.is_installed(bdir) is False
    _make_chromium(bdir)
    assert chromium.is_installed(bdir) is True


def test_prepare_env_sets_path_always_flag_only_when_present():
    chromium.prepare_env()
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == chromium.browsers_dir()
    # Not installed yet → flag must stay unset so a launch falls back to
    # system Chrome rather than a missing browser.
    assert "DLD_USE_BUNDLED_CHROMIUM" not in os.environ

    _make_chromium(chromium.browsers_dir())
    chromium.prepare_env()
    assert os.environ["DLD_USE_BUNDLED_CHROMIUM"] == "1"


def test_ensure_skips_install_when_present(monkeypatch):
    _make_chromium(chromium.browsers_dir())
    called = {"n": 0}
    monkeypatch.setattr(chromium, "_install", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True)
    assert chromium.ensure_chromium() is True
    assert called["n"] == 0
    assert os.environ["DLD_USE_BUNDLED_CHROMIUM"] == "1"


def test_ensure_installs_when_absent(monkeypatch):
    bdir = chromium.browsers_dir()

    def fake_install(d, progress):
        _make_chromium(d)
        return True

    monkeypatch.setattr(chromium, "_install", fake_install)
    assert chromium.ensure_chromium() is True
    assert chromium.is_installed(bdir) is True
    assert os.environ["DLD_USE_BUNDLED_CHROMIUM"] == "1"


def test_ensure_degrades_when_install_fails(monkeypatch):
    monkeypatch.setattr(chromium, "_install", lambda *a, **k: False)
    assert chromium.ensure_chromium() is False
    # Flag must NOT be set → chromium_launch_kwargs keeps channel='chrome'.
    assert "DLD_USE_BUNDLED_CHROMIUM" not in os.environ
    # But the browsers path is still pointed at our dir (harmless for channel).
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == chromium.browsers_dir()


def test_ensure_never_raises_on_install_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(chromium, "_install", boom)
    assert chromium.ensure_chromium() is False
    assert "DLD_USE_BUNDLED_CHROMIUM" not in os.environ


def test_driver_argv_handles_tuple_and_str(monkeypatch):
    import playwright._impl._driver as drv

    monkeypatch.setattr(drv, "compute_driver_executable", lambda: ("node", "cli.js"))
    assert chromium._driver_argv() == ["node", "cli.js"]

    monkeypatch.setattr(drv, "compute_driver_executable", lambda: "launcher")
    assert chromium._driver_argv() == ["launcher"]
