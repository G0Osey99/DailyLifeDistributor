import logging
from core import email


def test_render_returns_subject_html_text():
    subject, html, text = email.render_template(
        "welcome", username="alice", org_name="LCBC Church",
    )
    assert "alice" in html
    assert "alice" in text
    assert subject  # non-empty


def test_send_noops_without_api_key(monkeypatch, caplog):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING):
        ok = email.send("welcome", to="a@example.com", username="a", org_name="O")
    assert ok is False
    assert any("RESEND_API_KEY" in r.message for r in caplog.records)


def test_send_calls_resend_when_key_present(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_xxx")
    calls = []

    class _FakeEmails:
        @staticmethod
        def send(params):
            calls.append(params)
            return {"id": "fake-id"}

    class _FakeResend:
        api_key = None
        Emails = _FakeEmails

    monkeypatch.setattr(email, "resend", _FakeResend, raising=False)
    ok = email.send("welcome", to="b@example.com", username="b", org_name="O")
    assert ok is True
    assert len(calls) == 1
    assert calls[0]["to"] == ["b@example.com"]
    assert calls[0]["subject"]
    assert "html" in calls[0] and "text" in calls[0]


def test_render_unknown_template_raises():
    import pytest
    with pytest.raises(email.UnknownTemplateError):
        email.render_template("does_not_exist")


def test_render_invite_template():
    subject, html, text = email.render_template(
        "invite",
        org_name="LCBC",
        inviter_name="Bob",
        role="user",
        accept_url="https://x/a",
        agent_win_url="https://x/w",
        agent_mac_url="https://x/m",
    )
    assert "LCBC" in subject
    assert "LCBC" in html and "Bob" in html and "https://x/a" in html
    assert "Windows" in html and "macOS" in html
    assert "https://x/a" in text and "https://x/w" in text
    assert "https://x/m" in text


def test_render_welcome_with_agent_urls():
    subject, html, text = email.render_template(
        "welcome",
        username="alice",
        org_name="LCBC",
        role="manager",
        dashboard_url="https://example.com/dash",
        agent_win_url="https://example.com/w",
        agent_mac_url="https://example.com/m",
    )
    assert "LCBC" in subject
    assert "alice" in html and "manager" in html
    assert "Windows" in html and "macOS" in html
    assert "alice" in text
