"""_console must survive legacy Windows cp1252 consoles.

Live-reproduced: `print("✓ Connected …")` raised UnicodeEncodeError on a
cp1252 stdout, which the connection loop treated as a dropped connection —
the agent entered an endless connect → crash → reconnect storm.
"""
from __future__ import annotations

import io
import sys

import pytest


class _Cp1252Stdout(io.TextIOWrapper):
    pass


def _make_cp1252_stdout():
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")


def test_console_survives_cp1252(monkeypatch):
    from agent import main as agent_main

    fake = _make_cp1252_stdout()
    monkeypatch.setattr(sys, "stdout", fake)
    # Must not raise even though ✓ has no cp1252 mapping.
    agent_main._console("✓ Connected (TestDevice)")
    fake.seek(0)
    out = fake.buffer.getvalue().decode("cp1252")
    assert "Connected (TestDevice)" in out


def test_console_passes_through_plain_ascii(capsys):
    from agent import main as agent_main

    agent_main._console("plain message")
    assert "plain message" in capsys.readouterr().out
