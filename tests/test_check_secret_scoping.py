"""Lint: production code must scope every secrets_store accessor."""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_lint_flags_unscoped_call(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text(textwrap.dedent('''
        from core import secrets_store
        def boom():
            secrets_store.get_secret("youtube.token")  # no org_id
    '''))
    res = subprocess.run(
        [sys.executable, "scripts/check_secret_scoping.py", str(f)],
        capture_output=True, text=True,
    )
    assert res.returncode == 1
    assert "secrets_store.get_secret" in res.stdout


def test_lint_passes_scoped_call(tmp_path):
    f = tmp_path / "good.py"
    f.write_text(textwrap.dedent('''
        from core import secrets_store
        from core.org_context import effective_org_id
        def ok():
            secrets_store.get_secret("youtube.token", org_id=effective_org_id())
    '''))
    res = subprocess.run(
        [sys.executable, "scripts/check_secret_scoping.py", str(f)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0


def test_lint_passes_platform_call(tmp_path):
    f = tmp_path / "good.py"
    f.write_text("from core import secrets_store\nsecrets_store.get_platform_secret('x')\n")
    res = subprocess.run(
        [sys.executable, "scripts/check_secret_scoping.py", str(f)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0
