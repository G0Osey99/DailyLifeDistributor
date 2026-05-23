"""Relay account-keyed isolation.

The existing core.relay.Relay is already isolated by an account string —
phase β just needs callers to use ``f"org:{org_id}"`` instead of the
process-wide singleton ``"default"`` when more than one org is online.

These tests pin the isolation contract so a future caller-side refactor
that switches the account-key generator can't accidentally cross orgs.
The actual caller refactor (every blueprint/agent.py site that uses
``_ACCOUNT = "default"``) is deferred — it's a contained change with no
behaviour delta until two orgs co-exist on the same instance.
"""
from __future__ import annotations

from core.relay import Relay


def test_register_agent_then_broadcast_isolated_by_account():
    r = Relay()
    a_room1_sent: list[str] = []
    a_room2_sent: list[str] = []
    b_room1_sent: list[str] = []
    b_room2_sent: list[str] = []

    r.register_browser("org:1", "bs1", b_room1_sent.append)
    r.register_browser("org:2", "bs2", b_room2_sent.append)
    r.register_agent("org:1", "d1", a_room1_sent.append, "AgentA")
    r.register_agent("org:2", "d2", a_room2_sent.append, "AgentB")

    r.route_from_browser("org:1", "hello-org-1")
    assert "hello-org-1" in a_room1_sent
    assert not any("hello-org-1" in m for m in a_room2_sent)

    r.route_from_agent("org:2", "agent-says-org-2")
    assert "agent-says-org-2" in b_room2_sent
    assert not any("agent-says-org-2" in m for m in b_room1_sent)


def test_broadcast_to_browsers_account_scoped():
    r = Relay()
    org1: list[str] = []
    org2: list[str] = []
    r.register_browser("org:1", "s1", org1.append)
    r.register_browser("org:2", "s2", org2.append)
    r.broadcast_to_browsers("org:1", "relinked", {"who": "org1-only"})
    assert any("org1-only" in m for m in org1)
    assert not any("org1-only" in m for m in org2)


def test_agent_online_account_scoped():
    r = Relay()
    r.register_agent("org:1", "d", lambda _msg: None, "Agent")
    assert r.agent_online("org:1") is True
    assert r.agent_online("org:2") is False
