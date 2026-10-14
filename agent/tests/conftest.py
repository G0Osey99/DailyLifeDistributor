"""Isolation for agent tests.

Several agent tests exercise ``agent/dispatch.handle_job_plan``, which
installs the credentials + db shims by REPLACING ``sys.modules["core.db"]``
and ``sys.modules["core.secrets_store"]`` with proxy modules (that's the
agent's whole design — see agent/db_shim.py). In a test process those
replacements leaked: any test that ran afterwards and resolved
``from core import db`` got the proxy, whose every unknown attribute raises
NotImplementedError — e.g. tests/conftest.py's per-test ``db.init_db()``
blew up whenever agent/tests ran before tests/ in the same process.

Snapshot the real modules before each test and restore them after, so
shim installation can never escape the test that did it.
"""
from __future__ import annotations

import os
import sys

import pytest

_SHIMMED_MODULES = ("core.db", "core.secrets_store")


@pytest.fixture(autouse=True)
def _restore_shimmed_core_modules():
    saved = {name: sys.modules.get(name) for name in _SHIMMED_MODULES}
    yield
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


@pytest.fixture(autouse=True)
def _no_bundled_chromium_download(request, monkeypatch, tmp_path):
    """Never let a test download Chromium into the real ~/.dld-agent/browsers.

    agent/run_batch.run() calls agent.chromium.ensure_chromium(), which runs
    ``playwright install chromium`` (~150 MB) when no browser is present.
    Same guard as tests/conftest.py (see its docstring); opt out with
    ``@pytest.mark.real_chromium``.
    """
    from agent import chromium

    def _refuse_install(*_a, **_k):
        raise RuntimeError("tests must not download Chromium")

    def _tmp_browsers_dir():
        d = tmp_path / "dld-agent-browsers"
        d.mkdir(exist_ok=True)
        return str(d)

    monkeypatch.setattr(chromium, "_install", _refuse_install)
    if request.node.get_closest_marker("real_chromium") is None:
        monkeypatch.setattr(chromium, "browsers_dir", _tmp_browsers_dir)
        monkeypatch.setattr(chromium, "ensure_chromium", lambda progress=None: False)
    env_vars = ("PLAYWRIGHT_BROWSERS_PATH", "DLD_USE_BUNDLED_CHROMIUM")
    saved = {k: os.environ.get(k) for k in env_vars}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
