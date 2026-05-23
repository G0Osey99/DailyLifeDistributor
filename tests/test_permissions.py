import pytest
from flask import Flask, jsonify
from core import permissions, user_store


def _app():
    app = Flask(__name__)
    app.secret_key = "test"

    @app.route("/secret")
    @permissions.require_program_owner
    def secret():
        return jsonify({"ok": True})

    # Need auth.login endpoint for the redirect target. Provide a stub.
    @app.route("/login")
    def stub_login():
        return "login", 200
    app.view_functions["auth.login"] = stub_login
    # Register fake endpoint name "auth.login" so url_for works.
    app.add_url_rule("/login", endpoint="auth.login", view_func=stub_login)

    return app


def test_require_program_owner_blocks_anonymous():
    app = _app()
    with app.test_client() as c:
        resp = c.get("/secret")
        assert resp.status_code in (302, 403)  # redirect to login OR forbidden


def test_require_program_owner_blocks_non_owner():
    app = _app()
    u = user_store.create_user(username="u", email="u@x.com", password="pw12345678!")
    user_store.update_password(u["id"], "newpw1234567!")
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = u["id"]
        resp = c.get("/secret")
        assert resp.status_code == 403


def test_require_program_owner_allows_owner():
    app = _app()
    u = user_store.create_user(
        username="admin", email="a@x.com", password="pw12345678!",
        program_owner=True,
    )
    user_store.update_password(u["id"], "newpw1234567!")
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = u["id"]
        resp = c.get("/secret")
        assert resp.status_code == 200
