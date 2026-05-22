"""Tiny thread-safe circuit breaker for external integrations.

Why this exists
---------------
The upload runner (``core.upload_jobs.run_batch``) fans every selected date
out across a thread pool, dispatching one call per ``(date, platform)`` to
the platform uploaders. Most uploaders are Playwright browser automations
(SimpleCast / Vista Social / Rock). When a saved browser session is broken or
expired, *each* date relaunches Chrome and then blocks up to the per-platform
login timeout (``*_LOGIN_TIMEOUT``, default 300 s) before raising
``SessionExpiredError``. A 20-date run against one broken platform therefore
wastes ~100 minutes of worker time on a failure that was certain after the
first attempt — the textbook cascading failure.

A circuit breaker fixes this: after a small number of consecutive infra
failures the breaker *opens* and subsequent calls fail fast (no Chrome, no
wait) for a cool-down window, then allows a single trial call to probe whether
the dependency recovered.

Design
------
Three states, the canonical breaker state machine:

* ``CLOSED``    — calls allowed; consecutive failures are counted. Reaching
                  ``failure_threshold`` transitions to ``OPEN``.
* ``OPEN``      — calls rejected immediately. After ``recovery_timeout``
                  seconds, the next ``allow()`` transitions to ``HALF_OPEN``.
* ``HALF_OPEN`` — exactly one trial call is permitted. Success → ``CLOSED``
                  (counters reset); failure → ``OPEN`` (timer restarts).

The breaker is deliberately minimal and dependency-free. Callers drive it
explicitly with ``allow()`` / ``record_success()`` / ``record_failure()`` so
they can decide *which* outcomes count as a breaker failure (e.g. an infra
exception trips the breaker, but a per-row "video file not found" should not).
A convenience ``call()`` wrapper is provided for the simple all-or-nothing
case.

Breakers are shared by name through ``get_breaker(name)`` so every worker
thread dispatching the same platform consults the same instance.
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from typing import Callable, Dict, Optional, TypeVar

_log = logging.getLogger(__name__)

T = TypeVar("T")

# Sensible defaults for a slow, expensive integration (browser automation).
# Two consecutive failures is enough to conclude a session is broken without
# tripping on a single transient blip; the cool-down is long enough that we
# don't hammer a down dependency but short enough to recover within a run.
_DEFAULT_FAILURE_THRESHOLD = 3
_DEFAULT_RECOVERY_TIMEOUT = 120.0


class CircuitState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised by ``CircuitBreaker.call`` when the circuit is open."""

    def __init__(self, name: str):
        super().__init__(
            f"circuit '{name}' is open — failing fast after repeated failures"
        )
        self.name = name


class CircuitBreaker:
    """A single named circuit breaker. Thread-safe.

    Args:
        name: Identifier used in errors and logs.
        failure_threshold: Consecutive failures in CLOSED that open the circuit.
        recovery_timeout: Seconds the circuit stays OPEN before a trial call.
        clock: Monotonic time source (injectable for deterministic tests).
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        recovery_timeout: float = _DEFAULT_RECOVERY_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_timeout = max(0.0, float(recovery_timeout))
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None
        # True while a HALF_OPEN trial call is outstanding, so only one probe
        # runs at a time even under concurrent dispatch.
        self._trial_in_flight = False

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    def allow(self) -> bool:
        """Return True if a call may proceed, advancing state as needed.

        OPEN → HALF_OPEN happens here once ``recovery_timeout`` has elapsed,
        and reserves the single trial slot. Concurrent callers during a
        half-open probe are rejected until the probe records its outcome.
        """
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                opened = self._opened_at if self._opened_at is not None else self._clock()
                if (self._clock() - opened) >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._trial_in_flight = True
                    _log.info("circuit '%s' entering HALF_OPEN trial", self.name)
                    return True
                return False
            # HALF_OPEN: allow exactly one probe at a time.
            if self._trial_in_flight:
                return False
            self._trial_in_flight = True
            return True

    def record_success(self) -> None:
        """Reset the breaker to a healthy CLOSED state."""
        with self._lock:
            if self._state != CircuitState.CLOSED:
                _log.info("circuit '%s' closing after success", self.name)
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._trial_in_flight = False

    def record_failure(self) -> None:
        """Register a failure; open the circuit if the threshold is reached."""
        with self._lock:
            self._trial_in_flight = False
            if self._state == CircuitState.HALF_OPEN:
                # The probe failed — straight back to OPEN, restart the timer.
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
                _log.warning("circuit '%s' re-opened after failed trial", self.name)
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
                _log.warning(
                    "circuit '%s' OPEN after %d consecutive failures",
                    self.name, self._consecutive_failures,
                )

    def call(self, fn: Callable[..., T], *args, **kwargs) -> T:
        """Run ``fn`` through the breaker.

        Raises ``CircuitOpenError`` if the circuit is open. Any exception from
        ``fn`` is recorded as a failure and re-raised; a normal return records
        a success.
        """
        if not self.allow():
            raise CircuitOpenError(self.name)
        try:
            result = fn(*args, **kwargs)
        except BaseException:
            self.record_failure()
            raise
        self.record_success()
        return result

    def reset(self) -> None:
        """Force the breaker back to CLOSED (mainly for tests)."""
        self.record_success()


# ---------------------------------------------------------------------------
# Process-wide registry. Breakers are keyed by name so all worker threads
# dispatching the same integration share one instance.
# ---------------------------------------------------------------------------
_registry: Dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(
    name: str,
    *,
    failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
    recovery_timeout: float = _DEFAULT_RECOVERY_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
) -> CircuitBreaker:
    """Get-or-create the named breaker.

    The keyword args only take effect when the breaker is first created;
    later callers get the existing instance unchanged (standard registry
    semantics, so config is set by whoever touches it first).
    """
    with _registry_lock:
        br = _registry.get(name)
        if br is None:
            br = CircuitBreaker(
                name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
                clock=clock,
            )
            _registry[name] = br
        return br


def reset_all() -> None:
    """Drop every registered breaker. Used by the test suite for isolation."""
    with _registry_lock:
        _registry.clear()
