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
