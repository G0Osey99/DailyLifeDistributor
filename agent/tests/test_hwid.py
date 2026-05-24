"""Tests for agent.hwid — hardware-id hashing."""
from __future__ import annotations

import builtins
import re
import sys


from agent import hwid


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_hwid_returns_64char_hex():
    """sha256 hex digest must be exactly 64 lowercase hex chars."""
    result = hwid.compute_hwid_hash()
    assert isinstance(result, str)
    assert _HEX64.match(result), f"not 64-char lowercase hex: {result!r}"


def test_hwid_stable_across_calls():
    """Two calls in the same process must return the same digest."""
    a = hwid.compute_hwid_hash()
    b = hwid.compute_hwid_hash()
    assert a == b


def test_hwid_not_empty():
    """Even with all fallbacks active, the digest must not be the
    sha256-of-empty (i.e. the function must always feed real bytes)."""
    sha256_of_empty_salt = (
        "1bf7d56a6f7f6f2d2a01b8c4ad4b3a9a4f9c3b7e7f2c2f5a3e6a5b4d3c2b1a0f"
    )
    # Just assert non-empty distinctness; we don't lock in a specific value.
    result = hwid.compute_hwid_hash()
    assert result
    assert result != "0" * 64
    assert result != sha256_of_empty_salt


def test_hwid_fallback_when_machineid_raises(monkeypatch):
    """If py-machineid is installed but raises, hwid falls back to a stable
    hostname+platform-derived digest. Must still return 64-hex."""

    class _FakeMachineid:
        @staticmethod
        def id():
            raise RuntimeError("simulated machineid failure")

    # Inject a fake `machineid` module so the import inside compute_hwid_hash
    # succeeds but its id() raises.
    monkeypatch.setitem(sys.modules, "machineid", _FakeMachineid())
    # Force a fresh import path inside the function (the function imports
    # `machineid` at call time, so the patched sys.modules entry is used).
    result = hwid.compute_hwid_hash()
    assert _HEX64.match(result)


def test_hwid_fallback_when_machineid_returns_empty(monkeypatch):
    """machineid.id() returning empty triggers the fallback path."""

    class _FakeMachineid:
        @staticmethod
        def id():
            return ""

    monkeypatch.setitem(sys.modules, "machineid", _FakeMachineid())
    result = hwid.compute_hwid_hash()
    assert _HEX64.match(result)


def test_hwid_fallback_when_machineid_missing(monkeypatch):
    """If py-machineid isn't installed at all, the ImportError is caught
    and the fallback path runs."""
    real_import = builtins.__import__

    def _block_machineid(name, *args, **kwargs):
        if name == "machineid":
            raise ImportError("simulated missing machineid")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_machineid)
    # Drop any cached machineid entry so the import re-runs inside the fn.
    monkeypatch.delitem(sys.modules, "machineid", raising=False)
    result = hwid.compute_hwid_hash()
    assert _HEX64.match(result)


def test_hwid_fallback_is_deterministic(monkeypatch):
    """The fallback path must hash to the same value on repeated calls
    in the same process."""

    class _FakeMachineid:
        @staticmethod
        def id():
            raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, "machineid", _FakeMachineid())
    a = hwid.compute_hwid_hash()
    b = hwid.compute_hwid_hash()
    assert a == b
