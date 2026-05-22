"""Unit tests for core.circuit_breaker — state machine + registry."""
import pytest

from core.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    get_breaker,
    reset_all,
)


class _FakeClock:
    """Manually advanced monotonic clock for deterministic timing tests."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_closed_allows_and_counts_until_threshold():
    clock = _FakeClock()
    br = CircuitBreaker("t", failure_threshold=3, recovery_timeout=30, clock=clock)

    assert br.state is CircuitState.CLOSED
    assert br.allow() is True

    br.record_failure()
    assert br.state is CircuitState.CLOSED  # 1 < 3
    br.record_failure()
    assert br.state is CircuitState.CLOSED  # 2 < 3
    br.record_failure()
    assert br.state is CircuitState.OPEN    # 3 == threshold


def test_success_resets_failure_count():
    clock = _FakeClock()
    br = CircuitBreaker("t", failure_threshold=2, recovery_timeout=30, clock=clock)
    br.record_failure()
    br.record_success()         # counter back to 0
    br.record_failure()
    assert br.state is CircuitState.CLOSED  # only 1 failure since the reset


def test_open_rejects_until_recovery_timeout():
    clock = _FakeClock()
    br = CircuitBreaker("t", failure_threshold=1, recovery_timeout=30, clock=clock)
    br.record_failure()
    assert br.state is CircuitState.OPEN
    assert br.allow() is False              # still inside the cooldown

    clock.advance(29)
    assert br.allow() is False              # 29 < 30

    clock.advance(1)                        # now exactly at the timeout
    assert br.allow() is True               # transitions to HALF_OPEN trial
    assert br.state is CircuitState.HALF_OPEN


def test_half_open_single_trial_then_close_on_success():
    clock = _FakeClock()
    br = CircuitBreaker("t", failure_threshold=1, recovery_timeout=10, clock=clock)
    br.record_failure()
    clock.advance(10)

    assert br.allow() is True               # claims the one trial slot
    assert br.allow() is False              # second concurrent caller rejected
    br.record_success()
    assert br.state is CircuitState.CLOSED
    assert br.allow() is True               # fully healed


def test_half_open_failure_reopens_and_restarts_timer():
    clock = _FakeClock()
    br = CircuitBreaker("t", failure_threshold=1, recovery_timeout=10, clock=clock)
    br.record_failure()
    clock.advance(10)
    assert br.allow() is True               # HALF_OPEN trial
    br.record_failure()                     # probe failed
    assert br.state is CircuitState.OPEN
    assert br.allow() is False              # timer restarted, back in cooldown
    clock.advance(10)
    assert br.allow() is True               # probes again after another window


def test_call_wrapper_raises_when_open_and_records_outcomes():
    clock = _FakeClock()
    br = CircuitBreaker("t", failure_threshold=1, recovery_timeout=10, clock=clock)

    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        br.call(boom)               # failure recorded → opens (threshold 1)
    assert br.state is CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        br.call(lambda: "unreachable")

    clock.advance(10)
    assert br.call(lambda: "ok") == "ok"    # trial succeeds → closes
    assert br.state is CircuitState.CLOSED


def test_registry_returns_same_instance_and_resets():
    reset_all()
    a = get_breaker("svc", failure_threshold=5)
    b = get_breaker("svc")
    assert a is b
    # kwargs only apply on first creation
    assert a.failure_threshold == 5
    reset_all()
    c = get_breaker("svc")
    assert c is not a
