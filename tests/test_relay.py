from core.relay import Relay


class _Sink:
    def __init__(self):
        self.sent = []

    def __call__(self, text):
        self.sent.append(text)


def test_browser_ping_routed_to_agent():
    r = Relay()
    agent = _Sink()
    browser = _Sink()
    r.register_agent("acct", "dev1", agent)
    r.register_browser("acct", "sess1", browser)
    assert agent.sent == []  # registering a browser must not message the agent
    r.route_from_browser("acct", '{"v":1,"type":"ping","payload":{"x":1}}')
    assert agent.sent == ['{"v":1,"type":"ping","payload":{"x":1}}']


def test_agent_pong_routed_to_browsers():
    r = Relay()
    agent, browser = _Sink(), _Sink()
    r.register_agent("acct", "dev1", agent)
    r.register_browser("acct", "sess1", browser)
    browser.sent.clear()  # drop the on-connect presence snapshot
    r.route_from_agent("acct", '{"v":1,"type":"pong"}')
    assert browser.sent == ['{"v":1,"type":"pong"}']


def test_browser_gets_presence_on_connect_when_agent_online():
    # A browser connecting while an agent is already online must immediately
    # receive presence:online (Task 4's WS round-trip depends on this).
    r = Relay()
    r.register_agent("acct", "dev1", _Sink())
    browser = _Sink()
    r.register_browser("acct", "sess1", browser)
    assert any('"type": "presence"' in m and '"online": true' in m
               for m in browser.sent)


def test_presence_notifies_browsers_on_agent_connect():
    r = Relay()
    browser = _Sink()
    r.register_browser("acct", "sess1", browser)
    r.register_agent("acct", "dev1", _Sink())
    assert any('"type": "presence"' in m and '"online": true' in m
               for m in browser.sent)


def test_agent_online_flag():
    r = Relay()
    assert r.agent_online("acct") is False
    r.register_agent("acct", "dev1", _Sink())
    assert r.agent_online("acct") is True
    r.unregister_agent("acct", "dev1")
    assert r.agent_online("acct") is False


def test_unregister_browser_stops_delivery():
    r = Relay()
    agent, browser = _Sink(), _Sink()
    r.register_agent("acct", "dev1", agent)
    r.register_browser("acct", "sess1", browser)
    r.unregister_browser("acct", "sess1")
    browser.sent.clear()  # ignore the on-connect presence snapshot
    r.route_from_agent("acct", '{"v":1,"type":"pong"}')
    assert browser.sent == []


# ---------------------------------------------------------------------------
# Phase 3.5 — agent_ips + online_agents
# ---------------------------------------------------------------------------

def test_register_agent_stores_connect_ip():
    """register_agent(connect_ip=...) persists the IP on the room."""
    r = Relay()
    r.register_agent("acct", "dev1", _Sink(),
                     device_name="Mac", connect_ip="1.2.3.4")
    assert r.agent_ip("acct", "dev1") == "1.2.3.4"


def test_register_agent_without_connect_ip_keeps_none():
    """Omitting connect_ip is allowed (back-compat); lookup returns None."""
    r = Relay()
    r.register_agent("acct", "dev1", _Sink())
    assert r.agent_ip("acct", "dev1") is None


def test_unregister_agent_clears_ip():
    """unregister_agent removes the stored IP."""
    r = Relay()
    r.register_agent("acct", "dev1", _Sink(), connect_ip="1.2.3.4")
    r.unregister_agent("acct", "dev1")
    assert r.agent_ip("acct", "dev1") is None


def test_online_agents_lists_connected_with_ip_and_name():
    """online_agents returns one dict per connected agent."""
    r = Relay()
    r.register_agent("acct", "dev1", _Sink(),
                     device_name="Mac", connect_ip="1.2.3.4")
    r.register_agent("acct", "dev2", _Sink(),
                     device_name="Studio", connect_ip="5.6.7.8")
    out = r.online_agents("acct")
    by_id = {a["device_id"]: a for a in out}
    assert by_id["dev1"]["device_name"] == "Mac"
    assert by_id["dev1"]["connect_ip"] == "1.2.3.4"
    assert by_id["dev2"]["device_name"] == "Studio"
    assert by_id["dev2"]["connect_ip"] == "5.6.7.8"


def test_online_agents_empty_when_none_connected():
    """No agents → empty list (not error)."""
    r = Relay()
    assert r.online_agents("acct") == []


def test_online_agents_drops_unregistered():
    """unregister_agent removes the entry from online_agents."""
    r = Relay()
    r.register_agent("acct", "dev1", _Sink(), connect_ip="1.2.3.4")
    r.register_agent("acct", "dev2", _Sink(), connect_ip="5.6.7.8")
    r.unregister_agent("acct", "dev1")
    out = r.online_agents("acct")
    assert len(out) == 1
    assert out[0]["device_id"] == "dev2"


def test_agent_ip_unknown_room_returns_none():
    r = Relay()
    assert r.agent_ip("missing-account", "dev1") is None
