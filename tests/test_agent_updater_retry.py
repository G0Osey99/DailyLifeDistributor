"""Retry semantics of ``agent.updater._fetch_with_retry``.

Update-check runs once per agent boot, so:
  - One ConnectionError must not lock the agent into another boot cycle.
  - But more than one retry would block boot too long.
  - 4xx is deterministic (URL is wrong, asset doesn't exist) → no retry.
  - Both attempts exhausted re-raises the last exception so the caller
    can degrade gracefully.
"""
from __future__ import annotations

import pytest
import requests

from agent import updater


class _FakeResp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self):
        if 400 <= self.status_code < 600:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return {}


def test_returns_on_first_attempt_success(monkeypatch):
    calls = {"n": 0}

    def _get(url, **kw):
        calls["n"] += 1
        return _FakeResp(200)

    monkeypatch.setattr(updater.requests, "get", _get)
    r = updater._fetch_with_retry("https://x/manifest.json", timeout=5)
    assert r.status_code == 200
    assert calls["n"] == 1


def test_retries_once_on_connection_error_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def _get(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("boom")
        return _FakeResp(200)

    monkeypatch.setattr(updater.requests, "get", _get)
    # Don't actually sleep during the test.
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_kw: None)

    r = updater._fetch_with_retry("https://x/manifest.json", timeout=5)
    assert r.status_code == 200
    assert calls["n"] == 2


def test_http_error_does_not_retry(monkeypatch):
    calls = {"n": 0}

    def _get(url, **kw):
        calls["n"] += 1
        return _FakeResp(404)

    monkeypatch.setattr(updater.requests, "get", _get)
    with pytest.raises(requests.HTTPError):
        updater._fetch_with_retry("https://x/missing", timeout=5)
    assert calls["n"] == 1, "4xx must not retry"


def test_both_attempts_exhausted_reraises(monkeypatch):
    calls = {"n": 0}

    def _get(url, **kw):
        calls["n"] += 1
        raise requests.ConnectionError(f"attempt {calls['n']}")

    monkeypatch.setattr(updater.requests, "get", _get)
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_kw: None)

    with pytest.raises(requests.ConnectionError):
        updater._fetch_with_retry("https://x/manifest.json", timeout=5)
    assert calls["n"] == 2
