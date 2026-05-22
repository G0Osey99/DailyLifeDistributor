"""Resilience of the YouTube resumable-upload retry path.

`_next_chunk_with_retry` is what keeps a transient 5xx / network blip mid-upload
from failing (or worse, half-completing) a video. It was previously untested
(checklist T2). These tests exercise the classifier and the retry loop with a
fake request object so no real API calls happen.
"""
import pytest

from uploaders import youtube_uploader as yt


class _Resp:
    """Minimal stand-in for an httplib2 response carrying a status."""

    def __init__(self, status):
        self.status = status
        self.reason = "test-error"


def _http_error(status):
    from googleapiclient.errors import HttpError
    return HttpError(_Resp(status), b"{}")


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # The retry loop sleeps with exponential backoff; skip the wait in tests.
    monkeypatch.setattr(yt.time, "sleep", lambda *_a, **_k: None)


class _Req:
    """Fake resumable request: fail `fail_times` times, then return a chunk."""

    def __init__(self, fail_times, exc):
        self.calls = 0
        self.fail_times = fail_times
        self.exc = exc

    def next_chunk(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return ("STATUS", "RESPONSE")


def test_retryable_statuses_are_retryable():
    for status in (500, 502, 503, 504, 408, 429):
        assert yt._is_retryable_http_error(_http_error(status)) is True


def test_client_errors_are_not_retryable():
    for status in (400, 401, 403, 404):
        assert yt._is_retryable_http_error(_http_error(status)) is False


def test_non_httperror_is_not_retryable():
    assert yt._is_retryable_http_error(ValueError("nope")) is False


def test_retries_then_succeeds_on_retryable_http():
    req = _Req(fail_times=2, exc=_http_error(503))
    status, response = yt._next_chunk_with_retry(req)
    assert (status, response) == ("STATUS", "RESPONSE")
    assert req.calls == 3  # 2 failures + 1 success


def test_retries_on_transient_network_error():
    req = _Req(fail_times=1, exc=ConnectionError("connection reset"))
    status, response = yt._next_chunk_with_retry(req)
    assert (status, response) == ("STATUS", "RESPONSE")
    assert req.calls == 2


def test_non_retryable_http_raises_immediately():
    from googleapiclient.errors import HttpError
    req = _Req(fail_times=1, exc=_http_error(400))
    with pytest.raises(HttpError):
        yt._next_chunk_with_retry(req)
    assert req.calls == 1  # no retry on a 4xx


def test_exhausts_retries_then_raises():
    from googleapiclient.errors import HttpError
    req = _Req(fail_times=99, exc=_http_error(503))
    with pytest.raises(HttpError):
        yt._next_chunk_with_retry(req)
    assert req.calls == yt._MAX_RETRIES
