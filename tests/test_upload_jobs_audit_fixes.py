"""Regression tests for the web-path correctness fixes from the 2026-05-28 audit.

* WEB-8 — an element-DISABLED YouTube Video row must not be "expected" so the
  date's Rock Email falls back to entry.youtube_watch_url instead of erroring.
* WEB-13 — a SKIP must not heal the circuit breaker (only a genuine success).
"""
from __future__ import annotations

from core import upload_jobs
from core import circuit_breaker


# ---------------------------------------------------------------------------
# WEB-8 — element-skipped YouTube Video is not "expected"
# ---------------------------------------------------------------------------
def test_yt_video_expected_excludes_element_skipped_row():
    summary = [
        {"platform": "YouTube Video", "date": "Jun 1", "iso_date": "2026-06-01",
         "skipped": True},   # element-disabled → produces no watch URL
        {"platform": "YouTube Video", "date": "Jun 2", "iso_date": "2026-06-02",
         "skipped": False},  # active → produces a watch URL
    ]
    expected = upload_jobs._build_yt_video_expected(summary, skip_set=set())
    assert expected["2026-06-01"] is False, (
        "an element-skipped YT Video must not be 'expected' (Rock Email would "
        "otherwise wait forever instead of using the fallback URL)"
    )
    assert expected["2026-06-02"] is True


def test_yt_video_expected_excludes_idempotently_skipped_row():
    summary = [{"platform": "YouTube Video", "date": "Jun 1",
                "iso_date": "2026-06-01", "skipped": False}]
    skip_set = {"Jun 1_YouTube Video"}  # already a success in a prior run
    expected = upload_jobs._build_yt_video_expected(summary, skip_set=skip_set)
    assert expected["2026-06-01"] is False


# ---------------------------------------------------------------------------
# WEB-13 — a skip must not heal the breaker
# ---------------------------------------------------------------------------
def _entry_stub():
    from types import SimpleNamespace
    return SimpleNamespace(date="2026-06-01")


def test_skip_result_does_not_close_breaker(monkeypatch):
    # Force the platform's uploader to return a SKIP, and assert the breaker
    # does not record a success (which would mask a broken integration).
    breaker = circuit_breaker.get_breaker("upload:SimpleCast")
    breaker.reset()
    recorded = {"success": 0}
    orig = breaker.record_success

    def _counting_success():
        recorded["success"] += 1
        return orig()
    monkeypatch.setattr(breaker, "record_success", _counting_success)

    # Stub the SimpleCast uploader to return a skip. upload_jobs binds the
    # function at import (`sc_upload_episode`), so patch that name, not the
    # source module's attribute.
    monkeypatch.setattr(upload_jobs, "sc_upload_episode",
                        lambda *a, **k: {"skipped": True, "success": True})

    entry = _entry_stub()
    elements = type("E", (), {})()
    result = upload_jobs._dispatch_upload(
        "SimpleCast", entry, elements, lambda _f: None, 0,
        {"date": "2026-06-01", "iso_date": "2026-06-01"}, "2026-06-01", {})
    assert result.get("skipped") is True
    assert recorded["success"] == 0, "a skip must not heal the breaker"
