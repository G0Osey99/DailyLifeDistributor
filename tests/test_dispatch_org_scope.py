"""agent_dispatch._pick_device respects the per-org device pool.

A regular org's job must never dispatch to another org's agent.
A program-owner impersonating another org dispatches to the OWNER's own
devices only (not the impersonated org's devices) — the explicit support
pattern stated in the design.
"""
from __future__ import annotations

import pytest
from flask import Flask

from core import db, devices as _devices, org_store, user_store, agent_dispatch


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SECRET_ENC_KEY", "QcOzn6q5y8yp7v4OoH5sNcWZS_VqIyqU0o8jOwHjW6w=")
    import importlib
    import core.db, core.org_store, core.user_store, core.devices, core.agent_dispatch
    importlib.reload(core.db); importlib.reload(core.org_store)
    importlib.reload(core.user_store); importlib.reload(core.devices)
    importlib.reload(core.agent_dispatch)
    core.db.init_db()
    yield core.agent_dispatch, core.devices


@pytest.fixture()
def app_ctx():
    app = Flask(__name__); app.secret_key = "t"
    with app.test_request_context():
        yield


def _make_device(owner_user_id: int, device_id: str, name: str) -> None:
    """Insert a paired-device row owned by *owner_user_id*."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with db._get_conn() as c:
        c.execute(
            "INSERT INTO agent_devices "
            "(id, name, token_hash, created_at, last_seen_at, revoked, user_id) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (device_id, name, "th-" + device_id, now, now, owner_user_id),
        )
        c.commit()


def test_pick_device_excludes_other_orgs_agents(env, app_ctx, monkeypatch):
    """Org A's session must NOT pick org B's online agent."""
    ad, devs = env
    from flask import session
    org_a = org_store.create_org(name="A", slug="a")
    org_b = org_store.create_org(name="B", slug="b")
    user_a = user_store.create_user(username="ua", email="ua@x", password="pw1234567")
    user_b = user_store.create_user(username="ub", email="ub@x", password="pw1234567")
    org_store.add_membership(user_id=user_a["id"], org_id=org_a["id"], role="owner")
    org_store.add_membership(user_id=user_b["id"], org_id=org_b["id"], role="owner")
    _make_device(user_a["id"], "dev-A", "A mac")
    _make_device(user_b["id"], "dev-B", "B mac")

    # Only B's device is online; A's session asks for a pick.
    monkeypatch.setattr(
        ad, "_relay_online_agents",
        lambda: [{"device_id": "dev-B", "connect_ip": "1.1.1.1"}],
    )
    session["user_id"] = user_a["id"]
    session["current_org_id"] = org_a["id"]
    with pytest.raises(ad.NoAgentOnlineError):
        ad._pick_device()


def test_pick_device_returns_orgs_own_agent(env, app_ctx, monkeypatch):
    ad, devs = env
    from flask import session
    org = org_store.create_org(name="A", slug="a")
    user = user_store.create_user(username="u", email="u@x", password="pw1234567")
    org_store.add_membership(user_id=user["id"], org_id=org["id"], role="owner")
    _make_device(user["id"], "dev-A", "A mac")
    monkeypatch.setattr(
        ad, "_relay_online_agents",
        lambda: [{"device_id": "dev-A", "connect_ip": "1.1.1.1"}],
    )
    session["user_id"] = user["id"]
    session["current_org_id"] = org["id"]
    pick = ad._pick_device()
    assert pick["id"] == "dev-A"


def test_impersonation_picks_only_owners_device_not_targets(env, app_ctx, monkeypatch):
    """The program owner impersonating org B dispatches to THEIR own device,
    not org B's online device. This is the support pattern: the owner runs
    the job on their own machine with B's credentials shipped in the envelope."""
    ad, devs = env
    from flask import session
    boot = org_store.create_org(name="LCBC", slug="lcbc")
    target = org_store.create_org(name="Tgt", slug="tgt")
    po = user_store.create_user(
        username="po", email="po@x", password="pw1234567", program_owner=True,
    )
    other = user_store.create_user(username="ub", email="ub@x", password="pw1234567")
    org_store.add_membership(user_id=po["id"], org_id=boot["id"], role="owner")
    org_store.add_membership(user_id=other["id"], org_id=target["id"], role="owner")
    _make_device(po["id"], "dev-PO", "owner mac")
    _make_device(other["id"], "dev-TGT", "target mac")

    # Both online; impersonating Target.
    monkeypatch.setattr(
        ad, "_relay_online_agents",
        lambda: [
            {"device_id": "dev-PO",  "connect_ip": "1.1.1.1"},
            {"device_id": "dev-TGT", "connect_ip": "2.2.2.2"},
        ],
    )
    session["user_id"] = po["id"]
    session["current_org_id"] = boot["id"]
    session["acting_as_org_id"] = target["id"]
    pick = ad._pick_device()
    assert pick["id"] == "dev-PO", (
        "impersonation should dispatch to OWNER's device, not the target org's"
    )


def test_impersonation_with_no_owner_device_online_raises(env, app_ctx, monkeypatch):
    """While acting-as, if the owner has no device online, the dispatch
    must NOT silently fall back to the target org's agent — refuse the run
    so the owner sees the missing-agent error and starts theirs."""
    ad, devs = env
    from flask import session
    boot = org_store.create_org(name="LCBC", slug="lcbc")
    target = org_store.create_org(name="Tgt", slug="tgt")
    po = user_store.create_user(
        username="po", email="po@x", password="pw1234567", program_owner=True,
    )
    other = user_store.create_user(username="ub", email="ub@x", password="pw1234567")
    org_store.add_membership(user_id=po["id"], org_id=boot["id"], role="owner")
    org_store.add_membership(user_id=other["id"], org_id=target["id"], role="owner")
    _make_device(other["id"], "dev-TGT", "target mac")  # only target's agent

    monkeypatch.setattr(
        ad, "_relay_online_agents",
        lambda: [{"device_id": "dev-TGT", "connect_ip": "2.2.2.2"}],
    )
    session["user_id"] = po["id"]
    session["current_org_id"] = boot["id"]
    session["acting_as_org_id"] = target["id"]
    with pytest.raises(ad.NoAgentOnlineError):
        ad._pick_device()


def test_legacy_session_no_filter(env, app_ctx, monkeypatch):
    """LEGACY_PASSWORD_ENABLED session has no user_id and no current_org_id.
    The dispatch must NOT filter — the single-tenant USB install has one
    user and no tenant model; pre-multi-tenant behavior is preserved."""
    ad, devs = env
    user = user_store.create_user(username="u", email="u@x", password="pw1234567")
    _make_device(user["id"], "dev-X", "X mac")
    monkeypatch.setattr(
        ad, "_relay_online_agents",
        lambda: [{"device_id": "dev-X", "connect_ip": "1.1.1.1"}],
    )
    # No session keys set → legacy path.
    pick = ad._pick_device()
    assert pick["id"] == "dev-X"
