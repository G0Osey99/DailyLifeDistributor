import pytest
from core import org_store, user_store


def test_create_org_then_lookup():
    u = user_store.create_user(username="o", email="o@x.com", password="pw12345678!")
    org = org_store.create_org(
        name="LCBC Church", slug="lcbc-church", created_by_user_id=u["id"]
    )
    assert org["id"] >= 1
    assert org["name"] == "LCBC Church"
    assert org["slug"] == "lcbc-church"
    assert org["plan"] == "free"
    assert org["require_2fa"] == 0


def test_get_org_by_slug_and_id():
    u = user_store.create_user(username="o", email="o@x.com", password="pw12345678!")
    org = org_store.create_org(name="A", slug="a", created_by_user_id=u["id"])
    assert org_store.get_org_by_slug("a")["id"] == org["id"]
    assert org_store.get_org_by_id(org["id"])["slug"] == "a"
    assert org_store.get_org_by_slug("nope") is None


def test_list_orgs():
    u = user_store.create_user(username="o", email="o@x.com", password="pw12345678!")
    org_store.create_org(name="A", slug="a", created_by_user_id=u["id"])
    org_store.create_org(name="B", slug="b", created_by_user_id=u["id"])
    slugs = {o["slug"] for o in org_store.list_orgs()}
    assert {"a", "b"} <= slugs


def test_duplicate_slug_raises():
    u = user_store.create_user(username="o", email="o@x.com", password="pw12345678!")
    org_store.create_org(name="A", slug="a", created_by_user_id=u["id"])
    with pytest.raises(Exception):
        org_store.create_org(name="A2", slug="a", created_by_user_id=u["id"])


def test_add_membership_and_get():
    u = user_store.create_user(username="m", email="m@x.com", password="pw12345678!")
    org = org_store.create_org(name="O", slug="o", created_by_user_id=u["id"])
    mem = org_store.add_membership(user_id=u["id"], org_id=org["id"], role="owner")
    assert mem["role"] == "owner"
    assert mem["user_id"] == u["id"]
    assert mem["org_id"] == org["id"]
    got = org_store.get_membership(user_id=u["id"], org_id=org["id"])
    assert got["id"] == mem["id"]


def test_list_memberships_for_user_and_org():
    u1 = user_store.create_user(username="a", email="a@x.com", password="pw12345678!")
    u2 = user_store.create_user(username="b", email="b@x.com", password="pw12345678!")
    o1 = org_store.create_org(name="O1", slug="o1", created_by_user_id=u1["id"])
    o2 = org_store.create_org(name="O2", slug="o2", created_by_user_id=u1["id"])
    org_store.add_membership(user_id=u1["id"], org_id=o1["id"], role="owner")
    org_store.add_membership(user_id=u1["id"], org_id=o2["id"], role="manager")
    org_store.add_membership(user_id=u2["id"], org_id=o1["id"], role="user")
    user_orgs = {m["org_id"] for m in org_store.list_memberships_for_user(u1["id"])}
    assert user_orgs == {o1["id"], o2["id"]}
    org_members = {m["user_id"] for m in org_store.list_members_of_org(o1["id"])}
    assert org_members == {u1["id"], u2["id"]}


def test_change_role_and_remove():
    u = user_store.create_user(username="r", email="r@x.com", password="pw12345678!")
    o = org_store.create_org(name="O", slug="o", created_by_user_id=u["id"])
    org_store.add_membership(user_id=u["id"], org_id=o["id"], role="user")
    org_store.change_role(user_id=u["id"], org_id=o["id"], role="manager")
    assert org_store.get_membership(user_id=u["id"], org_id=o["id"])["role"] == "manager"
    org_store.remove_membership(user_id=u["id"], org_id=o["id"])
    assert org_store.get_membership(user_id=u["id"], org_id=o["id"]) is None
