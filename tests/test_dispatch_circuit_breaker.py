"""_dispatch_upload circuit-breaker behavior.

The real cascade this guards against: a broken/expired Playwright session makes
every date relaunch Chrome and block on the login timeout before raising
SessionExpiredError. After a few failures the breaker must fail fast.
"""
import pytest

from core import circuit_breaker, upload_jobs
from core.playwright_session import SessionExpiredError
from core.session_state import ReviewEntry


def _item(iso, platform):
    return {"date": iso, "iso_date": iso, "platform": platform, "title": "t"}


def _dispatch(platform, events, entry, iso):
    return upload_jobs._dispatch_upload(
        platform, entry, None, events.append, 0, _item(iso, platform), iso, {}
    )


def test_breaker_opens_after_repeated_session_expiry(monkeypatch):
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise SessionExpiredError("session expired")

    monkeypatch.setattr(upload_jobs, "sc_upload_episode", boom)
    # Pre-seed the breaker with a low threshold so the test is short; the
    # registry instance wins over the config-derived default.
    circuit_breaker.get_breaker("upload:SimpleCast", failure_threshold=2,
                                recovery_timeout=999)

    iso = "2025-05-21"
    entry = ReviewEntry(date=iso, display_date=iso)
    events: list = []

    # First two attempts reach the uploader and raise (recording failures).
    with pytest.raises(SessionExpiredError):
        _dispatch("SimpleCast", events, entry, iso)
    with pytest.raises(SessionExpiredError):
        _dispatch("SimpleCast", events, entry, iso)
    assert calls["n"] == 2

    # Breaker is now open: the third attempt fails fast without launching the
    # uploader at all.
    result = _dispatch("SimpleCast", events, entry, iso)
    assert calls["n"] == 2                       # uploader NOT called again
    assert result["success"] is False
    assert "temporarily disabled" in result["error"]
    assert any(e.get("type") == "phase_change" and e.get("phase") == "circuit_open"
               for e in events)


def test_one_broken_platform_does_not_disable_a_healthy_one(monkeypatch):
    def boom(*a, **k):
        raise SessionExpiredError("expired")

    healthy = {"n": 0}

    def ok(*a, **k):
        healthy["n"] += 1
        return {"success": True, "url": "https://yt/x"}

    monkeypatch.setattr(upload_jobs, "sc_upload_episode", boom)
    monkeypatch.setattr(upload_jobs, "yt_upload_video", ok)
    circuit_breaker.get_breaker("upload:SimpleCast", failure_threshold=1,
                                recovery_timeout=999)

    iso = "2025-05-21"
    entry = ReviewEntry(date=iso, display_date=iso)
    events: list = []

    with pytest.raises(SessionExpiredError):
        _dispatch("SimpleCast", events, entry, iso)
    # SimpleCast is now open, but YouTube is on its own breaker and runs fine.
    result = _dispatch("YouTube Video", events, entry, iso)
    assert result["success"] is True
    assert healthy["n"] == 1


def test_result_dict_failure_does_not_trip_breaker(monkeypatch):
    """A per-row data failure (dict with success=False, no exception) is
    neutral — it must not open the breaker."""
    def data_fail(*a, **k):
        return {"success": False, "error": "Video file not found"}

    monkeypatch.setattr(upload_jobs, "sc_upload_episode", data_fail)
    circuit_breaker.get_breaker("upload:SimpleCast", failure_threshold=2,
                                recovery_timeout=999)

    iso = "2025-05-21"
    entry = ReviewEntry(date=iso, display_date=iso)
    events: list = []

    for _ in range(5):
        result = _dispatch("SimpleCast", events, entry, iso)
        assert result["success"] is False
        assert "file not found" in result["error"]
    # Never tripped despite 5 dict-failures.
    assert circuit_breaker.get_breaker("upload:SimpleCast").state \
        is circuit_breaker.CircuitState.CLOSED
