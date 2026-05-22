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
