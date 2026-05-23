import pytest
from core import user_store, org_store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import importlib
    import app as flask_app_module
    importlib.reload(flask_app_module)
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def _seeded_user(memberships=1):
    u = user_store.create_user(
        username="sw", email="sw@x.com", password="pwbootstrap1234"
    )
    user_store.update_password(u["id"], "newpw12345678!")
    orgs = []
    for i in range(memberships):
        o = org_store.create_org(
            name=f"Org{i}", slug=f"org-{i}", created_by_user_id=u["id"]
        )
        org_store.add_membership(user_id=u["id"], org_id=o["id"], role="owner")
        orgs.append(o)
    return u, orgs


def test_switch_org_route_changes_session(client):
    u, orgs = _seeded_user(memberships=2)
    with client.session_transaction() as s:
        s["user_id"] = u["id"]
        s["current_org_id"] = orgs[0]["id"]
    resp = client.post(
        "/account/switch_org",
        data={"org_id": orgs[1]["id"]},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    with client.session_transaction() as s:
        assert s["current_org_id"] == orgs[1]["id"]


def test_switch_org_rejects_non_member(client):
    u, orgs = _seeded_user(memberships=1)
    with client.session_transaction() as s:
        s["user_id"] = u["id"]
        s["current_org_id"] = orgs[0]["id"]
    # Create an org the user is NOT a member of.
    other = org_store.create_org(
        name="Other", slug="other", created_by_user_id=u["id"]
    )
    resp = client.post(
        "/account/switch_org",
        data={"org_id": other["id"]},
    )
    assert resp.status_code == 403
    with client.session_transaction() as s:
        assert s["current_org_id"] == orgs[0]["id"]
