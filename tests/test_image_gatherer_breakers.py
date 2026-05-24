"""Circuit-breaker behavior for Unsplash, Pexels, and LLM-keyword paths.

`core/image_gatherer.py` guards every external call with a per-provider
breaker so that a flapping upstream doesn't burn the whole batch. After
three consecutive failures the breaker opens — subsequent calls must
return None / [] without issuing any HTTP request at all. These tests
pin that contract.
"""
from __future__ import annotations

import pytest
import requests

from core import circuit_breaker as _cb
from core import image_gatherer as ig


@pytest.fixture(autouse=True)
def _clean_breakers():
    """Reset the registry before AND rebind module-level breaker refs.

    Stub-failure tests record real failures into the singletons, but the
    conftest autouse fixture also wipes the registry around every test —
    that detaches the module's _UNSPLASH_BREAKER / etc. from whatever the
    registry now contains. Rebuild them so a fresh, registry-attached
    breaker is the one each call uses.
    """
    _cb.reset_all()
    ig._UNSPLASH_BREAKER = _cb.get_breaker(
        "image:unsplash", failure_threshold=3, recovery_timeout=120.0,
    )
    ig._PEXELS_BREAKER = _cb.get_breaker(
        "image:pexels", failure_threshold=3, recovery_timeout=120.0,
    )
    ig._LLM_KEYWORDS_BREAKER = _cb.get_breaker(
        "llm:title", failure_threshold=3, recovery_timeout=120.0,
    )
    yield
    _cb.reset_all()


# ----------------------------- Unsplash -------------------------------

def test_unsplash_breaker_opens_then_fails_fast_without_http(monkeypatch):
    """3 consecutive failures → breaker opens → 4th call is a no-op."""
    # Key must be present so the path runs past the env-check guard.
    monkeypatch.setattr(ig, "_resolve_key", lambda name: "fake-key")
    calls = {"n": 0}

    def _boom(url, *a, **kw):
        calls["n"] += 1
        raise requests.ConnectionError("simulated outage")

    monkeypatch.setattr(ig.requests, "get", _boom)

    # 3 failures trip the breaker (each call burns 2 attempts inside
    # _search_with_retry, but only one failure is recorded per call).
    for _ in range(3):
        assert ig._try_unsplash("sunset", "2026-01-01") is None
    assert ig._UNSPLASH_BREAKER.state == _cb.CircuitState.OPEN
    calls_after_open = calls["n"]

    # 4th call must short-circuit: no HTTP issued.
    result = ig._try_unsplash("sunset", "2026-01-01")
    assert result is None
    assert calls["n"] == calls_after_open, \
        "breaker should fail-fast without invoking requests.get"


# ----------------------------- Pexels ---------------------------------

def test_pexels_breaker_opens_then_fails_fast_without_http(monkeypatch):
    monkeypatch.setattr(ig, "_resolve_key", lambda name: "fake-key")
    calls = {"n": 0}

    def _boom(url, *a, **kw):
        calls["n"] += 1
        raise requests.ConnectionError("pexels down")

    monkeypatch.setattr(ig.requests, "get", _boom)

    for _ in range(3):
        assert ig._try_pexels("meadow", "2026-01-01") is None
    assert ig._PEXELS_BREAKER.state == _cb.CircuitState.OPEN
    calls_after_open = calls["n"]

    assert ig._try_pexels("meadow", "2026-01-01") is None
    assert calls["n"] == calls_after_open


# ----------------------------- LLM keywords ---------------------------

def test_llm_keywords_breaker_opens_then_skips_post(monkeypatch):
    """Three failed LLM keyword calls trip the breaker; the 4th doesn't POST."""
    # Make is_llamafile_running() return True so the function proceeds.
    monkeypatch.setattr(ig, "is_llamafile_running", lambda: True)
    posts = {"n": 0}

    def _boom_post(url, *a, **kw):
        posts["n"] += 1
        raise requests.ConnectionError("LLM upstream down")

    monkeypatch.setattr(ig.requests, "post", _boom_post)
    # No real sleeps between retry attempts.
    monkeypatch.setattr(ig.time, "sleep", lambda *_a, **_kw: None)

    for _ in range(3):
        assert ig._topic_terms_for_verse("Trust in the Lord") == []
    assert ig._LLM_KEYWORDS_BREAKER.state == _cb.CircuitState.OPEN
    posts_after_open = posts["n"]

    # Fourth call: breaker open, must skip the POST entirely.
    assert ig._topic_terms_for_verse("Be still") == []
    assert posts["n"] == posts_after_open, \
        "breaker should fail-fast without invoking requests.post"


def test_unsplash_breaker_allows_after_recovery(monkeypatch):
    """Sanity: HALF_OPEN trial succeeds → breaker closes."""
    monkeypatch.setattr(ig, "_resolve_key", lambda name: "fake-key")
    # Trip the breaker manually.
    for _ in range(3):
        ig._UNSPLASH_BREAKER.record_failure()
    assert ig._UNSPLASH_BREAKER.state == _cb.CircuitState.OPEN

    # Force the cool-down to zero so allow() advances to HALF_OPEN.
    ig._UNSPLASH_BREAKER._opened_at = 0.0  # type: ignore[attr-defined]

    # First call after recovery: stub a 200 response.
    class _FakeResp:
        status_code = 200

        def json(self):
            return {"results": []}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(ig.requests, "get", lambda *a, **kw: _FakeResp())
    # recent_photo_ids returns empty so the loop terminates cleanly.
    monkeypatch.setattr(ig._db, "recent_photo_ids", lambda *a, **kw: set())

    # Trial call: results list is empty, so no GatheredImage returned, but
    # the breaker should have recorded a success and re-closed.
    result = ig._try_unsplash("dawn", "2026-01-01")
    assert result is None
    assert ig._UNSPLASH_BREAKER.state == _cb.CircuitState.CLOSED
