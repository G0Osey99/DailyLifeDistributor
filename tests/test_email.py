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


# --- circuit-breaker behavior (added by external-integrations hardening) ----

def _reset_email_breaker():
    """Reset the module-level breaker between tests so state doesn't bleed."""
    email._RESEND_BREAKER.record_success()


def test_send_retries_once_on_transient_error(monkeypatch):
    _reset_email_breaker()
    monkeypatch.setenv("RESEND_API_KEY", "re_test_xxx")
    attempts = {"n": 0}

    class _FlakeyResend:
        api_key = ""

        class Emails:
            @staticmethod
            def send(payload):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise ConnectionError("simulated 5xx")
                return {"id": "ok"}

    monkeypatch.setattr(email, "resend", _FlakeyResend, raising=False)
    ok = email.send("welcome", to="x@x.com", username="x", org_name="O")
    assert ok is True
    assert attempts["n"] == 2  # one retry on transient


def test_send_does_not_retry_validation_errors(monkeypatch):
    _reset_email_breaker()
    monkeypatch.setenv("RESEND_API_KEY", "re_test_xxx")
    attempts = {"n": 0}

    class _ValidationResend:
        api_key = ""

        class Emails:
            @staticmethod
            def send(payload):
                attempts["n"] += 1
                raise ValueError("validation error: invalid recipient")

    monkeypatch.setattr(email, "resend", _ValidationResend, raising=False)
    ok = email.send("welcome", to="x@x.com", username="x", org_name="O")
    assert ok is False
    # No retry on validation-shaped error (would fail the same way).
    assert attempts["n"] == 1


def test_breaker_opens_after_repeated_failures_and_fails_fast(monkeypatch):
    _reset_email_breaker()
    monkeypatch.setenv("RESEND_API_KEY", "re_test_xxx")
    attempts = {"n": 0}

    class _DownResend:
        api_key = ""

        class Emails:
            @staticmethod
            def send(payload):
                attempts["n"] += 1
                raise ConnectionError("upstream 503")

    monkeypatch.setattr(email, "resend", _DownResend, raising=False)
    # Three consecutive failed sends (breaker threshold = 3).
    for _ in range(3):
        assert email.send("welcome", to="x@x.com", username="x", org_name="O") is False
    failures_before_breaker_open = attempts["n"]
    # Next call should be rejected by the breaker without touching resend.
    assert email.send("welcome", to="x@x.com", username="x", org_name="O") is False
    assert attempts["n"] == failures_before_breaker_open, \
        "breaker should have fast-failed without invoking resend"
