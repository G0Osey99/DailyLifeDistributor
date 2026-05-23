from flask import Flask, session
from core import auth


def _app():
    app = Flask(__name__)
    app.secret_key = "test"
    return app


def test_is_authenticated_via_user_id():
    app = _app()
    with app.test_request_context():
        assert auth.is_authenticated() is False
        session["user_id"] = 42
        assert auth.is_authenticated() is True


def test_is_authenticated_legacy_boolean_when_enabled(monkeypatch):
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "true")
    app = _app()
    with app.test_request_context():
        session["authenticated"] = True
        assert auth.is_authenticated() is True


def test_is_authenticated_legacy_boolean_ignored_when_flag_off(monkeypatch):
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
    app = _app()
    with app.test_request_context():
        session["authenticated"] = True
        assert auth.is_authenticated() is False


def test_current_user_id_and_current_org_id():
    app = _app()
    with app.test_request_context():
        assert auth.current_user_id() is None
        assert auth.current_org_id() is None
        session["user_id"] = 7
        session["current_org_id"] = 3
        assert auth.current_user_id() == 7
        assert auth.current_org_id() == 3
