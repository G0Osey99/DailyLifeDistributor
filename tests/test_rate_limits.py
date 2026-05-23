"""Rate-limit tests for /agent/pair/* and the ws connect/message buckets.

The autouse `_disable_rate_limiting` fixture in conftest.py turns the
limiter OFF for the rest of the suite — these tests explicitly turn it
back ON so we can verify the limits trip.
"""
from __future__ import annotations

import importlib
import time

import pytest


# ---------------------------------------------------------------------------
# Fixtures — full Flask app with rate limiting enabled
# ---------------------------------------------------------------------------


@pytest.fixture()
def rl_client(tmp_path, monkeypatch):
    """Client against a freshly-reloaded app with rate limiting enabled."""
    monkeypatch.setenv("RATELIMIT_ENABLED", "true")
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))

    # Reload the modules that capture env vars at import time so the new
    # values take effect.
    from core import auth
    import core.db as db
    import core.devices as devices
    importlib.reload(db)
    importlib.reload(devices)
    db.init_db()
    auth.reset_lockouts()
    auth.set_password("correct-horse")

    # Reload the agent blueprint + app so their fresh closures see the
    # new env / module reloads.
    import blueprints.agent as agent_bp_mod
    importlib.reload(agent_bp_mod)
    import app as flask_app_module
    importlib.reload(flask_app_module)
    flask_app_module.app.config["TESTING"] = True

    with flask_app_module.app.test_client() as c:
        yield c


def _login(c, password="correct-horse"):
    return c.post("/login", data={"password": password})


# ---------------------------------------------------------------------------
# /agent/pair/redeem — 5 per minute per IP
# ---------------------------------------------------------------------------


def test_pair_redeem_returns_429_after_burst(rl_client):
    """6th attempt within the same minute must 429."""
    # Each redeem with a bad code returns 400; that still consumes the limit.
    for _ in range(5):
        resp = rl_client.post("/agent/pair/redeem",
                              json={"code": "wrong", "name": "Mac"})
        assert resp.status_code in (400, 200)
    resp = rl_client.post("/agent/pair/redeem",
                          json={"code": "wrong", "name": "Mac"})
    assert resp.status_code == 429, (
        f"6th redeem in <60s should be 429, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# /agent/pair/new — 10 per hour per session
# ---------------------------------------------------------------------------


def test_pair_new_returns_429_after_burst(rl_client):
    """11th pair-new within the same hour for one session must 429."""
    _login(rl_client)
    for i in range(10):
        resp = rl_client.post("/agent/pair/new")
        assert resp.status_code == 200, f"call {i+1} failed: {resp.status_code}"
    resp = rl_client.post("/agent/pair/new")
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Per-WebSocket message token bucket — unit test the bucket directly
# ---------------------------------------------------------------------------


def test_token_bucket_caps_at_budget():
    """The token bucket allows up to WS_MSG_BUDGET in a window, then denies."""
    from blueprints.agent import _TokenBucket, WS_MSG_BUDGET

    b = _TokenBucket()
    for i in range(WS_MSG_BUDGET):
        assert b.allow() is True, f"msg {i+1} should pass"
    assert b.allow() is False, "msg over budget should be denied"


def test_token_bucket_refills_after_window():
    """After the window elapses, the bucket allows messages again."""
    from blueprints.agent import _TokenBucket

    # Tiny budget + window so the test is fast.
    b = _TokenBucket(budget=3, window=0.05)
    assert b.allow() and b.allow() and b.allow()
    assert b.allow() is False
    time.sleep(0.07)
    assert b.allow() is True


# ---------------------------------------------------------------------------
# Per-IP connect counter — unit-test the fixed-window counter
# ---------------------------------------------------------------------------


def test_fixed_window_counter_caps_at_budget():
    from blueprints.agent import _FixedWindowCounter

    c = _FixedWindowCounter(budget=3, window=60.0)
    assert c.allow("1.2.3.4") is True
    assert c.allow("1.2.3.4") is True
    assert c.allow("1.2.3.4") is True
    assert c.allow("1.2.3.4") is False  # 4th in window is denied
    # Different IP shares no counter.
    assert c.allow("5.6.7.8") is True


def test_fixed_window_counter_resets_after_window():
    from blueprints.agent import _FixedWindowCounter

    c = _FixedWindowCounter(budget=2, window=0.05)
    assert c.allow("k") is True
    assert c.allow("k") is True
    assert c.allow("k") is False
    time.sleep(0.07)
    assert c.allow("k") is True
