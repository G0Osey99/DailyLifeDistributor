"""Unit tests for the remote-login state machine (fake browser, no Chrome)."""
import pytest

from core import remote_login
from core.playwright_session import SessionConfig


class FakeBrowser:
    def __init__(self):
        self.goto_url = None
        self.closed = False
        self.saved_to = None

    def goto(self, url):
        self.goto_url = url

    def storage_state(self, path):
        self.saved_to = path
        with open(path, "w") as f:
            f.write('{"cookies": []}')

    def close(self):
        self.closed = True


def _cfg(tmp_path):
    return SessionConfig(
        name="simplecast",
        session_file=str(tmp_path / "simplecast_session.json"),
        is_login_url=lambda u: "login" in u,
        login_url="https://app.simplecast.com/login",
    )


@pytest.fixture
def mgr():
    created = []

    def launcher(config):
        b = FakeBrowser()
        created.append(b)
        return b

    m = remote_login.RemoteLoginManager(browser_launcher=launcher, idle_timeout_s=600)
    m._created = created  # test handle
    return m


def test_starts_idle(mgr):
    st = mgr.status()
    assert st.active is False
    assert st.phase == "idle"


def test_start_launches_and_navigates(mgr, tmp_path):
    mgr.start("simplecast", _cfg(tmp_path))
    st = mgr.status()
    assert st.active is True
    assert st.service == "simplecast"
    assert st.phase == "awaiting_login"
    assert mgr._created[0].goto_url == "https://app.simplecast.com/login"


def test_single_instance_lock(mgr, tmp_path):
    mgr.start("simplecast", _cfg(tmp_path))
    with pytest.raises(remote_login.RemoteLoginError):
        mgr.start("rock", _cfg(tmp_path))


def test_cancel_tears_down(mgr, tmp_path):
    mgr.start("simplecast", _cfg(tmp_path))
    browser = mgr._created[0]
    mgr.cancel()
    assert browser.closed is True
    assert mgr.status().active is False
    assert mgr.status().phase == "idle"


def test_idle_timeout_tears_down(tmp_path):
    fake_clock = {"t": 1000.0}

    def launcher(config):
        return FakeBrowser()

    m = remote_login.RemoteLoginManager(
        browser_launcher=launcher, idle_timeout_s=300,
        clock=lambda: fake_clock["t"],
    )
    m.start("simplecast", _cfg(tmp_path))
    fake_clock["t"] = 1000.0 + 301
    m.poll_timeout()
    assert m.status().active is False
    assert m.status().phase == "idle"


def test_save_success_flow(mgr, tmp_path, temp_db):
    cfg = _cfg(tmp_path)
    mgr.start("simplecast", cfg)
    browser = mgr._created[0]
    mgr.save()
    st = mgr.status()
    assert st.phase == "done"
    assert st.active is False
    assert browser.closed is True
    assert browser.saved_to == cfg.session_file


def test_save_failure_sets_error_and_tears_down(tmp_path, temp_db):
    class BoomBrowser(FakeBrowser):
        def storage_state(self, path):
            raise RuntimeError("boom")

    created = []

    def launcher(config):
        b = BoomBrowser()
        created.append(b)
        return b

    m = remote_login.RemoteLoginManager(browser_launcher=launcher)
    m.start("simplecast", _cfg(tmp_path))
    with pytest.raises(RuntimeError):
        m.save()
    st = m.status()
    assert st.phase == "error"
    assert "boom" in st.message
    assert created[0].closed is True
    assert st.active is False


def test_save_without_session_raises(mgr):
    with pytest.raises(remote_login.RemoteLoginError):
        mgr.save()
