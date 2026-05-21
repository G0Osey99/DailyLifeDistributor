"""core.vnc is a no-op off the hosted VPS (no Xvfb/x11vnc to drive)."""


def test_vnc_noop_when_not_hosted(monkeypatch):
    monkeypatch.delenv("HOSTED", raising=False)
    from core import vnc
    # Should not shell out to x11vnc; returns empty and stays empty.
    assert vnc.start_session() == ""
    assert vnc.current_password() == ""
    vnc.stop_session()
    assert vnc.current_password() == ""
