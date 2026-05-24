from flask import Flask, jsonify
from core import db, permissions, user_store


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


# ----- require_role -------------------------------------------------------

def _role_app():
    app = Flask(__name__)
    app.secret_key = "test"

    @app.route("/owners")
    @permissions.require_role("owner")
    def owners_only():
        return jsonify(ok=True)

    @app.route("/managers")
    @permissions.require_role("owner", "manager")
    def managers_only():
        return jsonify(ok=True)

    @app.route("/login")
    def stub_login():
        return "login", 200
    app.add_url_rule("/login", endpoint="auth.login", view_func=stub_login)
    return app


def _make_user_in_org(role: str, *, oid: int = 1, uid_hint: str = "u"):
    """Create a user + org + membership; return (user_id, org_id)."""
    u = user_store.create_user(
        username=f"{uid_hint}_{role}",
        email=f"{uid_hint}_{role}@x.com",
        password="long-enough-pw-12!",
    )
    user_store.update_password(u["id"], "long-enough-pw-12!")
    with db._get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO organizations (id, name, slug, plan, created_at) "
            "VALUES (?, ?, ?, 'free', datetime('now'))",
            (oid, f"Org{oid}", f"org-{oid}"),
        )
        c.execute(
            "INSERT INTO org_memberships (user_id, org_id, role, joined_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (u["id"], oid, role),
        )
        c.commit()
    return u["id"], oid


def test_require_role_allows_matching(monkeypatch):
    # Disable legacy bypass for this test
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
    uid, oid = _make_user_in_org("owner")
    app = _role_app()
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = uid
            s["current_org_id"] = oid
        r = c.get("/owners")
        assert r.status_code == 200


def test_require_role_denies_wrong_role(monkeypatch):
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
    uid, oid = _make_user_in_org("user")
    app = _role_app()
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = uid
            s["current_org_id"] = oid
        r = c.get("/owners")
        assert r.status_code == 403


def test_require_role_allows_manager_in_owner_or_manager(monkeypatch):
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
    uid, oid = _make_user_in_org("manager")
    app = _role_app()
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = uid
            s["current_org_id"] = oid
        r = c.get("/managers")
        assert r.status_code == 200


def test_program_owner_bypasses_role(monkeypatch):
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
    u = user_store.create_user(
        username="po", email="po@x.com",
        password="long-enough-pw-12!", program_owner=True,
    )
    user_store.update_password(u["id"], "long-enough-pw-12!")
    app = _role_app()
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = u["id"]
            # No current_org_id needed when program_owner.
        r = c.get("/owners")
        assert r.status_code == 200


def test_legacy_password_session_passes_through(monkeypatch):
    """LEGACY_PASSWORD_ENABLED=true sessions with `authenticated=True` but
    no `user_id` (the shared-password path) are permitted on @require_role."""
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "true")
    app = _role_app()
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["authenticated"] = True
        r = c.get("/owners")
        assert r.status_code == 200


def test_require_role_redirects_anonymous(monkeypatch):
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
    app = _role_app()
    with app.test_client() as c:
        r = c.get("/owners", follow_redirects=False)
        assert r.status_code == 302


def test_require_authenticated_json_returns_401(monkeypatch):
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
    app = Flask(__name__)
    app.secret_key = "t"

    @app.route("/api/x", methods=["POST"])
    @permissions.require_authenticated_json
    def x():
        return "ok", 200

    with app.test_client() as c:
        r = c.post("/api/x")
        assert r.status_code == 401
