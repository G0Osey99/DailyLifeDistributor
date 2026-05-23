# Hybrid Upload Agent — Phase 1 Implementation Plan

> **Status:** Shipped on 2026-05-23 (consolidated in the `codebase-completion-pass` branch — see git history for the actual per-commit work). The `- [ ]` checkboxes below are TDD step artifacts kept as-is for reference; all steps were executed.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the relay + device pairing layer so a logged-in browser and a paired local agent can exchange control messages through the VPS over `wss`, with revocable per-device tokens and agent-presence tracking — no uploading yet.

**Architecture:** Add a WebSocket relay hub to the existing sync Flask app (via `flask-sock`). Browser and agent each open a socket to the VPS; the relay joins them into an account-scoped room and forwards small JSON envelopes. Device auth is a pairing-code → revocable token flow. A minimal Python agent (isolated `agent/` package, never imported by the server) proves the round-trip by replying `pong` to a browser `ping`.

**Tech Stack:** Python 3.11+, Flask, `flask-sock` (server WS) + `simple-websocket` (client WS, pulled in by flask-sock), SQLite (`core/db.py`), `keyring` (agent token storage), `pytest`.

**Scope note:** This is Phase 1 of the larger spec (`docs/superpowers/specs/2026-05-22-hybrid-upload-agent-design.md`). It deliberately stops before job dispatch and uploads. The relay/agent code is additive and gated behind `HYBRID_AGENT_ENABLED`; the web-only flow is untouched.

---

## File Structure

**Server (VPS):**
- `core/db.py` — *modify*: add `agent_devices` + `agent_pairing_codes` tables in `init_db()`.
- `core/devices.py` — *create*: pairing-code + device-token model (generate, redeem, verify, list, revoke, touch). Pure DB logic, no Flask.
- `core/relay.py` — *create*: in-memory relay hub (account-scoped rooms, register/unregister, route, presence). Pure logic; sockets injected as `send` callables. Thread-safe.
- `blueprints/agent.py` — *create*: HTTP pairing routes (Blueprint `agent`) + the two WebSocket handlers + a `register_sockets(sock, relay)` function.
- `app.py` — *modify*: init `flask-sock`, register the agent blueprint + sockets behind `HYBRID_AGENT_ENABLED`, extend `_PUBLIC_ENDPOINTS`.
- `requirements.txt` — *modify*: add `flask-sock`.

**Agent (isolated, cross-OS — never imported by the server):**
- `agent/__init__.py` — *create*: empty package marker.
- `agent/config.py` — *create*: server URL + media roots (JSON file) and device token (OS keychain via `keyring`).
- `agent/pair.py` — *create*: redeem a pairing code over HTTP → store the token.
- `agent/transport.py` — *create*: `wss` client (connect with token, hello handshake, send/recv JSON, reconnect).
- `agent/main.py` — *create*: entrypoint — pair if needed, connect, reply `pong` to `ping` (Phase 1 proof).
- `agent/requirements.txt` — *create*: `simple-websocket`, `requests`, `keyring`.

**Tests:**
- `tests/test_devices.py`, `tests/test_relay.py`, `tests/test_agent_pairing_routes.py`, `tests/test_agent_transport.py`, `tests/integration/test_relay_roundtrip.py`.

**Message envelope (all control messages):**
```json
{"v": 1, "type": "ping|pong|hello|presence|error", "payload": { } }
```

---

### Task 1: Device + pairing-code data model

**Files:**
- Modify: `core/db.py` (inside `init_db()`, after the `external_calendar_items` block)
- Create: `core/devices.py`
- Test: `tests/test_devices.py`

- [ ] **Step 1: Add the tables to `init_db()`**

In `core/db.py`, inside `init_db()` immediately after the `external_calendar_items` index creation, add:

```python
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_devices (
                id TEXT PRIMARY KEY,
                name TEXT,
                token_hash TEXT NOT NULL,
                created_at TEXT,
                last_seen_at TEXT,
                revoked INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_pairing_codes (
                code_hash TEXT PRIMARY KEY,
                created_at TEXT,
                expires_at TEXT,
                consumed INTEGER NOT NULL DEFAULT 0
            )
        """)
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_devices.py`:

```python
import time
from core import db, devices


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    # Re-point the module-level path used by both modules.
    import importlib
    importlib.reload(db)
    importlib.reload(devices)
    db.init_db()


def test_redeem_valid_code_creates_device(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code(ttl_seconds=300)
    assert isinstance(code, str) and len(code) >= 8
    device_id, token = devices.redeem_pairing_code(code, "Ryker-Mac")
    assert device_id and token
    assert devices.verify_device_token(token) == device_id


def test_code_is_single_use(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    devices.redeem_pairing_code(code, "dev1")
    assert devices.redeem_pairing_code(code, "dev2") is None


def test_expired_code_rejected(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code(ttl_seconds=0)
    time.sleep(0.01)
    assert devices.redeem_pairing_code(code, "dev1") is None


def test_revoked_token_fails_verify(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    device_id, token = devices.redeem_pairing_code(code, "dev1")
    devices.revoke_device(device_id)
    assert devices.verify_device_token(token) is None


def test_list_devices_reports_revoked(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    code = devices.create_pairing_code()
    device_id, _ = devices.redeem_pairing_code(code, "dev1")
    devices.revoke_device(device_id)
    rows = devices.list_devices()
    assert any(r["id"] == device_id and r["revoked"] == 1 for r in rows)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_devices.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.devices'`.

- [ ] **Step 4: Implement `core/devices.py`**

```python
"""Agent device pairing + revocable token model (backed by state.db).

Pairing codes and device tokens are stored as SHA-256 hashes; the raw values
are returned to the caller exactly once. This mirrors how core.auth stores the
shared password hash.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from core.db import _get_conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_pairing_code(ttl_seconds: int = 600) -> str:
    """Mint a single-use pairing code valid for ttl_seconds."""
    code = secrets.token_urlsafe(9)  # ~12 chars, URL-safe
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_pairing_codes (code_hash, created_at, expires_at, consumed) "
            "VALUES (?, ?, ?, 0)",
            (_hash(code), now.isoformat(),
             (now + timedelta(seconds=ttl_seconds)).isoformat()),
        )
        conn.commit()
    return code


def redeem_pairing_code(code: str, device_name: str) -> tuple[str, str] | None:
    """Consume a valid code, create a device, return (device_id, raw_token).

    Returns None if the code is unknown, expired, or already consumed.
    """
    code_hash = _hash(code)
    now = _now()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT expires_at, consumed FROM agent_pairing_codes WHERE code_hash = ?",
            (code_hash,),
        ).fetchone()
        if row is None or row["consumed"]:
            return None
        if datetime.fromisoformat(row["expires_at"]) < now:
            return None
        conn.execute(
            "UPDATE agent_pairing_codes SET consumed = 1 WHERE code_hash = ?",
            (code_hash,),
        )
        device_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO agent_devices (id, name, token_hash, created_at, last_seen_at, revoked) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (device_id, device_name or "device", _hash(token),
             now.isoformat(), now.isoformat()),
        )
        conn.commit()
    return device_id, token


def verify_device_token(token: str) -> str | None:
    """Return the device_id for a valid, non-revoked token, else None."""
    if not token:
        return None
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM agent_devices WHERE token_hash = ? AND revoked = 0",
            (_hash(token),),
        ).fetchone()
        return row["id"] if row else None


def touch_device(device_id: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE agent_devices SET last_seen_at = ? WHERE id = ?",
            (_now().isoformat(), device_id),
        )
        conn.commit()


def revoke_device(device_id: str) -> None:
    with _get_conn() as conn:
        conn.execute("UPDATE agent_devices SET revoked = 1 WHERE id = ?", (device_id,))
        conn.commit()


def list_devices() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, last_seen_at, revoked "
            "FROM agent_devices ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_devices.py -q`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add core/db.py core/devices.py tests/test_devices.py
git commit -m "feat(agent): device pairing-code + revocable token model"
```

---

### Task 2: Pairing HTTP routes

**Files:**
- Create: `blueprints/agent.py` (HTTP routes only in this task; sockets added in Task 4)
- Test: `tests/test_agent_pairing_routes.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_pairing_routes.py`:

```python
import pytest
from core import auth


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    import importlib
    import core.db as db
    import core.devices as devices
    importlib.reload(db)
    importlib.reload(devices)
    db.init_db()
    auth.reset_lockouts()
    auth.set_password("correct-horse")
    import app as flask_app_module
    importlib.reload(flask_app_module)
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def _login(c):
    c.post("/login", data={"password": "correct-horse"})


def test_pair_new_requires_auth(client):
    resp = client.post("/agent/pair/new")
    assert resp.status_code in (302, 401)  # redirect to login or JSON 401


def test_pair_redeem_roundtrip(client):
    _login(client)
    code = client.post("/agent/pair/new").get_json()["code"]
    # Redeem is public (the agent has no session yet).
    client2 = client.application.test_client()
    resp = client2.post("/agent/pair/redeem", json={"code": code, "name": "Mac"})
    assert resp.status_code == 200
    assert resp.get_json()["device_id"]
    assert resp.get_json()["token"]


def test_list_and_revoke(client):
    _login(client)
    code = client.post("/agent/pair/new").get_json()["code"]
    client.application.test_client().post(
        "/agent/pair/redeem", json={"code": code, "name": "Mac"})
    devs = client.get("/agent/devices").get_json()["devices"]
    assert len(devs) == 1
    did = devs[0]["id"]
    assert client.post(f"/agent/devices/{did}/revoke").status_code == 200
    devs = client.get("/agent/devices").get_json()["devices"]
    assert devs[0]["revoked"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_agent_pairing_routes.py -q`
Expected: FAIL (404s — routes don't exist yet / blueprint not registered).

- [ ] **Step 3: Create `blueprints/agent.py` with the HTTP routes**

```python
"""Hybrid upload agent: device pairing HTTP routes + WebSocket relay.

Phase 1 — pairing/token endpoints and the relay sockets. No uploads yet.
The blueprint and sockets are only registered when HYBRID_AGENT_ENABLED.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core import devices

bp = Blueprint("agent", __name__)


@bp.route("/agent/pair/new", methods=["POST"])
def pair_new():
    """Generate a single-use pairing code (session-gated by _require_auth)."""
    code = devices.create_pairing_code()
    return jsonify({"code": code})


@bp.route("/agent/pair/redeem", methods=["POST"])
def pair_redeem():
    """Redeem a pairing code for a device token (no session — agent has none yet)."""
    data = request.get_json(silent=True) or {}
    result = devices.redeem_pairing_code(
        (data.get("code") or "").strip(), (data.get("name") or "device").strip())
    if result is None:
        return jsonify({"error": "invalid or expired code"}), 400
    device_id, token = result
    return jsonify({"device_id": device_id, "token": token})


@bp.route("/agent/devices", methods=["GET"])
def list_devices():
    return jsonify({"devices": devices.list_devices()})


@bp.route("/agent/devices/<device_id>/revoke", methods=["POST"])
def revoke_device(device_id):
    devices.revoke_device(device_id)
    return jsonify({"ok": True})
```

- [ ] **Step 4: Register the blueprint + make redeem public (in `app.py`)**

In `app.py` `create_app()`, alongside the other `register_blueprint` calls (near line 355-361), add:

```python
    if os.environ.get("HYBRID_AGENT_ENABLED", "").lower() in ("1", "true", "yes"):
        from blueprints.agent import bp as agent_bp
        app.register_blueprint(agent_bp)
```

And extend `_PUBLIC_ENDPOINTS` (line ~184) so the agent can redeem without a session:

```python
    _PUBLIC_ENDPOINTS = {"auth.login", "auth.login_submit", "_health", "static",
                         "agent.pair_redeem"}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_agent_pairing_routes.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add blueprints/agent.py app.py tests/test_agent_pairing_routes.py
git commit -m "feat(agent): pairing HTTP routes behind HYBRID_AGENT_ENABLED"
```

---

### Task 3: Relay hub (pure logic)

**Files:**
- Create: `core/relay.py`
- Test: `tests/test_relay.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_relay.py`:

```python
from core.relay import Relay


class _Sink:
    def __init__(self):
        self.sent = []

    def __call__(self, text):
        self.sent.append(text)


def test_browser_ping_routed_to_agent():
    r = Relay()
    agent = _Sink()
    browser = _Sink()
    r.register_agent("acct", "dev1", agent)
    r.register_browser("acct", "sess1", browser)
    r.route_from_browser("acct", '{"v":1,"type":"ping","payload":{"x":1}}')
    assert agent.sent == ['{"v":1,"type":"ping","payload":{"x":1}}']


def test_agent_pong_routed_to_browsers():
    r = Relay()
    agent, browser = _Sink(), _Sink()
    r.register_agent("acct", "dev1", agent)
    r.register_browser("acct", "sess1", browser)
    r.route_from_agent("acct", '{"v":1,"type":"pong"}')
    assert browser.sent == ['{"v":1,"type":"pong"}']


def test_presence_notifies_browsers_on_agent_connect():
    r = Relay()
    browser = _Sink()
    r.register_browser("acct", "sess1", browser)
    r.register_agent("acct", "dev1", _Sink())
    assert any('"type": "presence"' in m and '"online": true' in m
               for m in browser.sent)


def test_agent_online_flag():
    r = Relay()
    assert r.agent_online("acct") is False
    r.register_agent("acct", "dev1", _Sink())
    assert r.agent_online("acct") is True
    r.unregister_agent("acct", "dev1")
    assert r.agent_online("acct") is False


def test_unregister_browser_stops_delivery():
    r = Relay()
    agent, browser = _Sink(), _Sink()
    r.register_agent("acct", "dev1", agent)
    r.register_browser("acct", "sess1", browser)
    r.unregister_browser("acct", "sess1")
    r.route_from_agent("acct", '{"v":1,"type":"pong"}')
    assert browser.sent == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_relay.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.relay'`.

- [ ] **Step 3: Implement `core/relay.py`**

```python
"""In-memory relay hub joining browsers and agents into account-scoped rooms.

A `sink` is any callable taking one str (the raw JSON message) — in production
it wraps a flask-sock connection's .send; in tests it's a list-appender. The
relay never parses message bodies except to stamp presence; it just forwards.
"""
from __future__ import annotations

import json
import threading
from typing import Callable

Sink = Callable[[str], None]


class _Room:
    def __init__(self) -> None:
        self.agents: dict[str, Sink] = {}      # device_id -> sink
        self.browsers: dict[str, Sink] = {}    # session_id -> sink


class Relay:
    def __init__(self) -> None:
        self._rooms: dict[str, _Room] = {}
        self._lock = threading.RLock()

    def _room(self, account: str) -> _Room:
        return self._rooms.setdefault(account, _Room())

    # ---- registration -------------------------------------------------
    def register_agent(self, account: str, device_id: str, sink: Sink) -> None:
        with self._lock:
            self._room(account).agents[device_id] = sink
        self._broadcast_presence(account, online=True)

    def unregister_agent(self, account: str, device_id: str) -> None:
        with self._lock:
            self._room(account).agents.pop(device_id, None)
            still_online = bool(self._room(account).agents)
        self._broadcast_presence(account, online=still_online)

    def register_browser(self, account: str, session_id: str, sink: Sink) -> None:
        with self._lock:
            self._room(account).browsers[session_id] = sink
        # Tell the freshly-connected browser the current agent status.
        online = self.agent_online(account)
        sink(json.dumps({"v": 1, "type": "presence", "payload": {"online": online}}))

    def unregister_browser(self, account: str, session_id: str) -> None:
        with self._lock:
            self._room(account).browsers.pop(session_id, None)

    # ---- routing ------------------------------------------------------
    def route_from_browser(self, account: str, message: str) -> None:
        with self._lock:
            sinks = list(self._room(account).agents.values())
        for s in sinks:
            s(message)

    def route_from_agent(self, account: str, message: str) -> None:
        with self._lock:
            sinks = list(self._room(account).browsers.values())
        for s in sinks:
            s(message)

    # ---- presence -----------------------------------------------------
    def agent_online(self, account: str) -> bool:
        with self._lock:
            return bool(self._rooms.get(account) and self._rooms[account].agents)

    def _broadcast_presence(self, account: str, online: bool) -> None:
        msg = json.dumps({"v": 1, "type": "presence", "payload": {"online": online}})
        with self._lock:
            sinks = list(self._room(account).browsers.values())
        for s in sinks:
            s(msg)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_relay.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add core/relay.py tests/test_relay.py
git commit -m "feat(agent): account-scoped relay hub with presence"
```

---

### Task 4: WebSocket endpoints + server wiring

**Files:**
- Modify: `blueprints/agent.py` (add `register_sockets`)
- Modify: `app.py` (init flask-sock, register sockets, public endpoint, shared Relay)
- Modify: `requirements.txt`
- Test: `tests/integration/test_relay_roundtrip.py`

- [ ] **Step 1: Add `flask-sock` to requirements and install**

In `requirements.txt` add after the `playwright` line:

```
# Server-side WebSocket for the hybrid-agent relay (pulls in simple-websocket).
flask-sock>=0.7,<1
```

Run: `pip install "flask-sock>=0.7,<1"`
Expected: installs `flask-sock` and `simple-websocket`.

- [ ] **Step 2: Add the socket handlers + `register_sockets` to `blueprints/agent.py`**

Append to `blueprints/agent.py`:

```python
import json as _json

from flask import session as _session

from core import relay as _relay_mod
from core.devices import verify_device_token, touch_device

# One process-wide relay shared by all sockets.
RELAY = _relay_mod.Relay()

# Single shared account for now (shared-password deploy). Future multi-tenant
# work keys rooms by real account id.
_ACCOUNT = "default"
_AUTH_SESSION_KEY = "authenticated"


def register_sockets(sock) -> None:
    """Register the agent + browser WebSocket routes on a flask_sock.Sock."""

    @sock.route("/agent/socket")
    def agent_socket(ws):
        token = request.args.get("token", "")
        device_id = verify_device_token(token)
        if not device_id:
            ws.send(_json.dumps({"v": 1, "type": "error",
                                 "payload": {"reason": "unauthorized"}}))
            return
        touch_device(device_id)
        RELAY.register_agent(_ACCOUNT, device_id, ws.send)
        try:
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                RELAY.route_from_agent(_ACCOUNT, msg)
        finally:
            RELAY.unregister_agent(_ACCOUNT, device_id)

    @sock.route("/agent/ws")
    def agent_browser_socket(ws):
        # Session-authenticated (the global _require_auth lets the upgrade GET
        # through only for logged-in browsers; double-check here too).
        if not _session.get(_AUTH_SESSION_KEY):
            ws.send(_json.dumps({"v": 1, "type": "error",
                                 "payload": {"reason": "unauthorized"}}))
            return
        session_id = _json.dumps(id(ws))  # unique per connection
        RELAY.register_browser(_ACCOUNT, session_id, ws.send)
        try:
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                RELAY.route_from_browser(_ACCOUNT, msg)
        finally:
            RELAY.unregister_browser(_ACCOUNT, session_id)
```

- [ ] **Step 3: Wire flask-sock into `app.py`**

In `app.py` replace the Task-2 registration block with:

```python
    if os.environ.get("HYBRID_AGENT_ENABLED", "").lower() in ("1", "true", "yes"):
        from flask_sock import Sock
        from blueprints.agent import bp as agent_bp, register_sockets
        app.register_blueprint(agent_bp)
        sock = Sock(app)
        register_sockets(sock)
```

Extend `_PUBLIC_ENDPOINTS` so the agent's token-authed socket bypasses session auth (the browser socket `agent_browser_socket` stays gated):

```python
    _PUBLIC_ENDPOINTS = {"auth.login", "auth.login_submit", "_health", "static",
                         "agent.pair_redeem", "agent_socket"}
```

- [ ] **Step 4: Write the failing integration test**

Create `tests/integration/test_relay_roundtrip.py`:

```python
"""Browser <-> VPS relay <-> agent control round-trip over real WebSockets."""
import json
import threading
import time

import pytest

simple_websocket = pytest.importorskip("simple_websocket")


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    import importlib
    import core.db as db
    import core.devices as devices
    importlib.reload(db); importlib.reload(devices); db.init_db()
    from core import auth
    auth.reset_lockouts(); auth.set_password("pw")
    import app as flask_app_module
    importlib.reload(flask_app_module)
    app = flask_app_module.app
    app.config["TESTING"] = True

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    port = srv.socket.getsockname()[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)
    yield {"app": app, "port": port, "devices": devices}
    srv.shutdown()


def test_ping_pong_through_relay(live_server):
    port = live_server["port"]
    devices = live_server["devices"]

    # Pair a device directly via the model (HTTP redeem covered elsewhere).
    code = devices.create_pairing_code()
    device_id, token = devices.redeem_pairing_code(code, "Mac")

    # Browser must be logged in: get a session cookie, then connect its socket.
    import requests
    s = requests.Session()
    s.post(f"http://127.0.0.1:{port}/login", data={"password": "pw"})
    cookie = "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())

    agent = simple_websocket.Client(f"ws://127.0.0.1:{port}/agent/socket?token={token}")
    browser = simple_websocket.Client(
        f"ws://127.0.0.1:{port}/agent/ws", headers={"Cookie": cookie})

    # Browser should get presence=online for the agent.
    presence = json.loads(browser.receive(timeout=5))
    assert presence["type"] == "presence" and presence["payload"]["online"] is True

    # Browser pings; agent receives it and replies pong; browser gets pong.
    browser.send(json.dumps({"v": 1, "type": "ping", "payload": {"x": 1}}))
    got = json.loads(agent.receive(timeout=5))
    assert got["type"] == "ping" and got["payload"]["x"] == 1
    agent.send(json.dumps({"v": 1, "type": "pong", "payload": {"x": 1}}))
    pong = json.loads(browser.receive(timeout=5))
    assert pong["type"] == "pong" and pong["payload"]["x"] == 1

    agent.close(); browser.close()
```

- [ ] **Step 5: Run the integration test to verify it fails, then passes**

Run: `python -m pytest tests/integration/test_relay_roundtrip.py -q`
Expected first: FAIL (sockets not registered / import error). After Steps 2-3 are in place, re-run.
Expected: PASS (1 passed).

- [ ] **Step 6: Run the full suite (no regressions)**

Run: `python -m pytest -q`
Expected: PASS, with the 3 pre-existing live integration tests skipped.

- [ ] **Step 7: Commit**

```bash
git add blueprints/agent.py app.py requirements.txt tests/integration/test_relay_roundtrip.py
git commit -m "feat(agent): wss relay endpoints + browser/agent round-trip"
```

---

### Task 5: Agent config + token storage

**Files:**
- Create: `agent/__init__.py`, `agent/config.py`, `agent/requirements.txt`
- Test: `tests/test_agent_config.py`

- [ ] **Step 1: Create the agent package + requirements**

Create `agent/__init__.py` (empty). Create `agent/requirements.txt`:

```
simple-websocket>=1.0,<2
requests>=2.32.2,<3
keyring>=24,<26
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_agent_config.py`:

```python
import json
from agent import config


class _MemKeyring:
    def __init__(self): self.store = {}
    def set_password(self, svc, user, pw): self.store[(svc, user)] = pw
    def get_password(self, svc, user): return self.store.get((svc, user))
    def delete_password(self, svc, user): self.store.pop((svc, user), None)


def test_token_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_keyring", _MemKeyring())
    monkeypatch.setattr(config, "_CONFIG_PATH", str(tmp_path / "agent.json"))
    config.set_token("abc123")
    assert config.get_token() == "abc123"


def test_config_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_CONFIG_PATH", str(tmp_path / "agent.json"))
    config.set_server_url("https://autoalert.pro")
    config.set_media_roots({"video": "/Users/x/vids"})
    assert config.get_server_url() == "https://autoalert.pro"
    assert config.get_media_roots() == {"video": "/Users/x/vids"}
    # Persisted as JSON on disk.
    data = json.load(open(tmp_path / "agent.json"))
    assert data["server_url"] == "https://autoalert.pro"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_config.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.config'`.

- [ ] **Step 4: Implement `agent/config.py`**

```python
"""Agent-side config: server URL + media roots in a JSON file; device token in
the OS keychain (via keyring). The token never touches the JSON file."""
from __future__ import annotations

import json
import os

import keyring as _keyring

_SERVICE = "dld-hybrid-agent"
_TOKEN_USER = "device-token"
_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".dld-agent", "agent.json")


def _load() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def set_token(token: str) -> None:
    _keyring.set_password(_SERVICE, _TOKEN_USER, token)


def get_token() -> str | None:
    return _keyring.get_password(_SERVICE, _TOKEN_USER)


def clear_token() -> None:
    try:
        _keyring.delete_password(_SERVICE, _TOKEN_USER)
    except Exception:
        pass


def set_server_url(url: str) -> None:
    d = _load(); d["server_url"] = url.rstrip("/"); _save(d)


def get_server_url() -> str | None:
    return _load().get("server_url")


def set_media_roots(roots: dict) -> None:
    d = _load(); d["media_roots"] = roots; _save(d)


def get_media_roots() -> dict:
    return _load().get("media_roots", {})
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_config.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add agent/__init__.py agent/config.py agent/requirements.txt tests/test_agent_config.py
git commit -m "feat(agent): config file + keychain token storage"
```

---

### Task 6: Agent pairing client

**Files:**
- Create: `agent/pair.py`
- Test: `tests/test_agent_pair_client.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_pair_client.py`:

```python
from agent import pair, config


def test_redeem_stores_token(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200
        def json(self): return {"device_id": "d1", "token": "tok-xyz"}

    def fake_post(url, json, timeout):
        captured["url"] = url; captured["json"] = json
        return _Resp()

    monkeypatch.setattr(pair.requests, "post", fake_post)
    monkeypatch.setattr(config, "set_token", lambda t: captured.setdefault("token", t))

    ok = pair.redeem("https://autoalert.pro", "CODE123", "Mac")
    assert ok is True
    assert captured["url"] == "https://autoalert.pro/agent/pair/redeem"
    assert captured["json"] == {"code": "CODE123", "name": "Mac"}
    assert captured["token"] == "tok-xyz"


def test_redeem_failure_returns_false(monkeypatch):
    class _Resp:
        status_code = 400
        def json(self): return {"error": "invalid or expired code"}

    monkeypatch.setattr(pair.requests, "post", lambda *a, **k: _Resp())
    assert pair.redeem("https://x", "BAD", "Mac") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_pair_client.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.pair'`.

- [ ] **Step 3: Implement `agent/pair.py`**

```python
"""Redeem a pairing code over HTTP and store the resulting device token."""
from __future__ import annotations

import requests

from agent import config


def redeem(server_url: str, code: str, device_name: str, timeout: float = 15.0) -> bool:
    """POST the code to the server; on success store the token + server URL."""
    resp = requests.post(
        server_url.rstrip("/") + "/agent/pair/redeem",
        json={"code": code, "name": device_name},
        timeout=timeout,
    )
    if resp.status_code != 200:
        return False
    token = resp.json().get("token")
    if not token:
        return False
    config.set_token(token)
    config.set_server_url(server_url)
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_pair_client.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add agent/pair.py tests/test_agent_pair_client.py
git commit -m "feat(agent): pairing-code redeem client"
```

---

### Task 7: Agent transport client

**Files:**
- Create: `agent/transport.py`
- Test: `tests/test_agent_transport.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_transport.py`:

```python
import json
from agent import transport


class _FakeWS:
    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent = []
        self.closed = False
    def send(self, text): self.sent.append(text)
    def receive(self, timeout=None):
        return self._incoming.pop(0) if self._incoming else None
    def close(self): self.closed = True


def test_handshake_sends_hello(monkeypatch):
    fake = _FakeWS(incoming=[])
    monkeypatch.setattr(transport, "_connect", lambda url: fake)
    conn = transport.AgentConnection("https://autoalert.pro", "tok")
    conn.connect()
    sent = json.loads(fake.sent[0])
    assert sent["type"] == "hello" and sent["v"] == transport.PROTOCOL_VERSION


def test_url_uses_wss_and_token(monkeypatch):
    captured = {}
    monkeypatch.setattr(transport, "_connect",
                        lambda url: captured.setdefault("url", url) or _FakeWS([]))
    transport.AgentConnection("https://autoalert.pro", "tok-9").connect()
    assert captured["url"] == "wss://autoalert.pro/agent/socket?token=tok-9"


def test_run_handles_ping_with_handler(monkeypatch):
    fake = _FakeWS(incoming=[json.dumps({"v": 1, "type": "ping", "payload": {"x": 7}})])
    monkeypatch.setattr(transport, "_connect", lambda url: fake)
    conn = transport.AgentConnection("https://x", "t")
    conn.connect()
    seen = []
    conn.run_once(lambda msg: seen.append(msg))
    assert seen == [{"v": 1, "type": "ping", "payload": {"x": 7}}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_transport.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.transport'`.

- [ ] **Step 3: Implement `agent/transport.py`**

```python
"""Outbound wss client to the VPS relay. Sends a hello handshake, then a
receive loop that hands each decoded message to a callback. Reconnect with
backoff is the caller's concern via connect()/run_once()."""
from __future__ import annotations

import json
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import simple_websocket

PROTOCOL_VERSION = 1


def _connect(url: str):
    """Seam for tests: real WebSocket client."""
    return simple_websocket.Client(url)


def _to_ws_url(server_url: str, token: str) -> str:
    parts = urlsplit(server_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, "/agent/socket", f"token={token}", ""))


class AgentConnection:
    def __init__(self, server_url: str, token: str):
        self.server_url = server_url
        self.token = token
        self.ws = None

    def connect(self) -> None:
        self.ws = _connect(_to_ws_url(self.server_url, self.token))
        self.ws.send(json.dumps({"v": PROTOCOL_VERSION, "type": "hello",
                                 "payload": {"role": "agent"}}))

    def send(self, message: dict) -> None:
        self.ws.send(json.dumps(message))

    def run_once(self, on_message: Callable[[dict], None]) -> bool:
        """Receive one message and dispatch it. Returns False when closed."""
        raw = self.ws.receive(timeout=None)
        if raw is None:
            return False
        on_message(json.loads(raw))
        return True

    def close(self) -> None:
        if self.ws:
            self.ws.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_transport.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add agent/transport.py tests/test_agent_transport.py
git commit -m "feat(agent): wss transport client with hello handshake"
```

---

### Task 8: Agent entrypoint + full-stack proof

**Files:**
- Create: `agent/main.py`
- Test: `tests/integration/test_agent_end_to_end.py`

- [ ] **Step 1: Implement `agent/main.py`**

```python
"""Phase 1 agent entrypoint: pair if needed, connect, reply pong to ping.

Run:  python -m agent.main --server https://autoalert.pro
First run prompts for a pairing code (generated in the web UI).
"""
from __future__ import annotations

import argparse
import json
import socket
import time

from agent import config, pair
from agent.transport import AgentConnection


def _device_name() -> str:
    return socket.gethostname() or "device"


def _ensure_paired(server_url: str) -> str:
    token = config.get_token()
    if token:
        return token
    code = input("Enter pairing code from the website: ").strip()
    if not pair.redeem(server_url, code, _device_name()):
        raise SystemExit("Pairing failed — check the code and try again.")
    return config.get_token()


def _on_message(conn: AgentConnection, msg: dict) -> None:
    if msg.get("type") == "ping":
        conn.send({"v": 1, "type": "pong", "payload": msg.get("payload", {})})


def run(server_url: str) -> None:
    token = _ensure_paired(server_url)
    while True:
        conn = AgentConnection(server_url, token)
        try:
            conn.connect()
            while conn.run_once(lambda m: _on_message(conn, m)):
                pass
        except Exception:  # noqa: BLE001 — reconnect on any drop
            pass
        finally:
            conn.close()
        time.sleep(3)  # backoff before reconnect


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=config.get_server_url() or "https://autoalert.pro")
    args = ap.parse_args()
    run(args.server)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the full-stack integration test**

Create `tests/integration/test_agent_end_to_end.py`:

```python
"""End-to-end: real server + agent.main pong logic + browser ws client."""
import json
import threading
import time

import pytest

simple_websocket = pytest.importorskip("simple_websocket")


@pytest.fixture()
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    import importlib
    import core.db as db, core.devices as devices
    importlib.reload(db); importlib.reload(devices); db.init_db()
    from core import auth
    auth.reset_lockouts(); auth.set_password("pw")
    import app as m; importlib.reload(m)
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, m.app, threaded=True)
    port = srv.socket.getsockname()[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield port, devices
    srv.shutdown()


def test_agent_main_pong(live, monkeypatch):
    port, devices = live
    server_url = f"http://127.0.0.1:{port}"

    # Pair via model; inject token + server into agent.config.
    code = devices.create_pairing_code()
    _, token = devices.redeem_pairing_code(code, "Mac")
    from agent import config
    monkeypatch.setattr(config, "get_token", lambda: token)
    monkeypatch.setattr(config, "get_server_url", lambda: server_url)

    # Start agent.run in a thread (it loops/reconnects; daemon thread is fine).
    from agent import main as agent_main
    threading.Thread(target=agent_main.run, args=(server_url,), daemon=True).start()
    time.sleep(0.5)

    import requests
    s = requests.Session(); s.post(f"{server_url}/login", data={"password": "pw"})
    cookie = "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())
    browser = simple_websocket.Client(f"ws://127.0.0.1:{port}/agent/ws",
                                      headers={"Cookie": cookie})
    json.loads(browser.receive(timeout=5))  # presence
    browser.send(json.dumps({"v": 1, "type": "ping", "payload": {"n": 42}}))
    pong = json.loads(browser.receive(timeout=5))
    assert pong["type"] == "pong" and pong["payload"]["n"] == 42
    browser.close()
```

- [ ] **Step 3: Run the test to verify it passes**

Run: `python -m pytest tests/integration/test_agent_end_to_end.py -q`
Expected: PASS (1 passed).

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS; pre-existing live integration tests skipped.

- [ ] **Step 5: Commit**

```bash
git add agent/main.py tests/integration/test_agent_end_to_end.py
git commit -m "feat(agent): Phase 1 entrypoint + end-to-end ping/pong proof"
```

---

## Phase 1 Acceptance

When all tasks are complete:
- A logged-in browser can `POST /agent/pair/new` to mint a code; the agent redeems it for a revocable device token stored in the OS keychain.
- The agent connects outbound over `ws`/`wss`; the server tracks presence and a logged-in browser receives `presence:{online:true}`.
- A browser `ping` is relayed to the agent, which replies `pong`, relayed back to the browser — proving bidirectional control round-trips through the VPS.
- Devices can be listed and revoked from `/agent/devices`; a revoked token fails `verify_device_token`.
- Everything is gated behind `HYBRID_AGENT_ENABLED`; with the flag off, the app behaves exactly as before (full suite green, web-only flow untouched).

## Deferred to later phases (not this plan)
- Job dispatch / plan builder, session-blob push, and running real uploaders (Phases 3-4).
- Media-root scanning + available-dates reporting (Phase 2).
- Auto-update framework + signed release feed (Phase 2).
- Coexistence UI ("Fast upload (this device)") + packaging/signing (Phase 5).
- Production WebSocket pass-through verification (Cloudflare Tunnel + Caddy) before enabling the flag on the VPS.
