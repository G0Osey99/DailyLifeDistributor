import pytest
from core import user_store, org_store


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import importlib
    import app as flask_app_module
    importlib.reload(flask_app_module)
    flask_app_module.app.config["TESTING"] = True
    return flask_app_module.app


def _owner_login(client):
    u = user_store.create_user(
        username="root", email="root@x.com", password="pwbootstrap1234",
        program_owner=True,
    )
    user_store.update_password(u["id"], "newadminpw12345")
    with client.session_transaction() as s:
        s["user_id"] = u["id"]
    return u


def _user_login(client):
    u = user_store.create_user(
        username="joe", email="joe@x.com", password="pwbootstrap1234",
    )
    user_store.update_password(u["id"], "newpw12345678")
    with client.session_transaction() as s:
        s["user_id"] = u["id"]
    return u


def test_admin_landing_requires_program_owner(app):
    with app.test_client() as c:
        _user_login(c)
        resp = c.get("/admin")
        assert resp.status_code == 403


def test_admin_landing_redirects_to_org_list(app):
    with app.test_client() as c:
        _owner_login(c)
        resp = c.get("/admin", follow_redirects=False)
        assert resp.status_code == 302
        assert "/admin/organizations" in resp.headers.get("Location", "")


def test_admin_organizations_list(app):
    with app.test_client() as c:
        owner = _owner_login(c)
        # Use a distinct slug so the autouse migration's "lcbc-church" doesn't
        # collide with the per-test fixture insert.
        org_store.create_org(
            name="Test Church", slug="test-church",
            created_by_user_id=owner["id"],
        )
        resp = c.get("/admin/organizations")
        assert resp.status_code == 200
        assert b"Test Church" in resp.data


def test_admin_organizations_create(app):
    with app.test_client() as c:
        _owner_login(c)
        resp = c.post(
            "/admin/organizations",
            data={"name": "Acme Corp", "slug": "acme-corp"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert org_store.get_org_by_slug("acme-corp") is not None


def test_admin_users_list_shows_all(app):
    with app.test_client() as c:
        _owner_login(c)
        user_store.create_user(username="x", email="x@x.com", password="pw12345678!")
        resp = c.get("/admin/users")
        assert resp.status_code == 200
        assert b"x" in resp.data


def test_admin_force_reset_clears_password_changed_at(app):
    from core import db as _db
    with app.test_client() as c:
        _owner_login(c)
        target = user_store.create_user(
            username="resetme", email="r@x.com", password="pw12345678!",
        )
        user_store.update_password(target["id"], "anotherpw12345")
        assert _db.get_user_by_id(target["id"])["password_changed_at"] is not None
        resp = c.post(
            "/admin/users/force_reset",
            data={"user_id": str(target["id"])},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        after = _db.get_user_by_id(target["id"])
        assert after["password_changed_at"] is None


def test_admin_force_reset_refuses_other_program_owner(app):
    from core import db as _db
    with app.test_client() as c:
        _owner_login(c)
        # Second program-owner — should NOT be force-resettable by the first.
        other = user_store.create_user(
            username="root2", email="root2@x.com", password="pwbootstrap1234",
            program_owner=True,
        )
        user_store.update_password(other["id"], "anotherpw12345")
        before = _db.get_user_by_id(other["id"])["password_changed_at"]
        resp = c.post(
            "/admin/users/force_reset",
            data={"user_id": str(other["id"])},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        # Nothing changed for the other program-owner.
        after = _db.get_user_by_id(other["id"])["password_changed_at"]
        assert after == before


def test_admin_force_reset_writes_audit(app):
    from core import db as _db
    with app.test_client() as c:
        _owner_login(c)
        target = user_store.create_user(
            username="audited", email="a@x.com", password="pw12345678!",
        )
        user_store.update_password(target["id"], "anotherpw12345")
        c.post(
            "/admin/users/force_reset",
            data={"user_id": str(target["id"])},
        )
        rows = _db.list_audit_events(
            action_prefix="user.force_password_reset", limit=10,
        )
        assert any(int(r["target_id"]) == target["id"] for r in rows)
