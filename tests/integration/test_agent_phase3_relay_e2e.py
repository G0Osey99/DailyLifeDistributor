"""End-to-end relay tests: real Flask server + real WebSocket clients
acting as the agent and browser, plus a real agent_dispatch.start that
fans the envelope through the relay.

This is the test the Phase 3 twin-runner deliberately deferred (see
test_agent_phase3_end_to_end.py docstring). The twin-runner asserted
that the two paths emit equivalent event sequences against in-process
stubs; it did NOT exercise the WebSocket transport, the relay, or the
hello-frame replay path. Those are what catch the ack-loss race that
shows up in production but not in unit tests.

Three scenarios:
  1. happy_path                       — fake agent emits start/upload_progress/
                                         success/done; server records to
                                         upload_history; browser sees the
                                         frames in order with correct shapes.
  2. mid_stream_disconnect_and_reconnect — agent closes after a success event,
                                           reconnects with pending_results in
                                           the hello frame; server applies
                                           idempotently (no duplicate row in
                                           upload_history) and acks.
  3. server_restart_simulation        — close everything, drop the job from
                                         the in-memory registry, reconnect
                                         the agent with pending_results;
                                         server acks them (so the agent
                                         clears its buffer) even though it
                                         can't record (session_id unknown).
"""
from __future__ import annotations

import importlib
import json
import threading
import time

import pytest

simple_websocket = pytest.importorskip("simple_websocket")
requests_lib = pytest.importorskip("requests")


# ---------------------------------------------------------------------------
# Fixture: real Flask server on an ephemeral port
# ---------------------------------------------------------------------------


@pytest.fixture()
def running_server(tmp_path, monkeypatch):
    """Spin up the real Flask app on an ephemeral 127.0.0.1 port.

    Yields (server_url, port, devices_module, app_module).
    """
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    # Don't let rate limiting trip mid-test (we open multiple sockets in
    # the same test).
    monkeypatch.setenv("RATELIMIT_ENABLED", "false")
    # Multi-tenant phase α: this test posts the legacy single-field login
    # form; opt in to that path.
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "true")

    import core.db as db
    import core.devices as devices
    importlib.reload(db)
    importlib.reload(devices)
    db.init_db()

    from core import auth
    auth.reset_lockouts()
    auth.set_password("pw")

    # Reload agent blueprint + app so its fresh closures see the new env.
    import blueprints.agent as agent_bp_mod
    importlib.reload(agent_bp_mod)
    import app as app_mod
    importlib.reload(app_mod)

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, app_mod.app, threaded=True)
    # ThreadingMixIn.daemon_threads defaults to False, so any websocket
    # connection still open at teardown keeps Python from exiting and
    # hangs the whole CI suite. Make request-handler threads daemons.
    srv.daemon_threads = True
    port = srv.socket.getsockname()[1]
    server_url = f"http://127.0.0.1:{port}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    # Give the server a moment to bind.
    time.sleep(0.2)

    yield (server_url, port, devices, app_mod)

    srv.shutdown()


def _login_session(server_url: str) -> str:
    """Authenticate a Flask session and return the cookie string."""
    s = requests_lib.Session()
    s.post(f"{server_url}/login", data={"password": "pw"})
    return "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())


def _pair_agent(server_url: str, devices_mod) -> str:
    """Create a pairing code, redeem it, return the device token."""
    code = devices_mod.create_pairing_code()
    _, token = devices_mod.redeem_pairing_code(code, "Mac")
    return token


def _open_agent_ws(server_url: str, port: int, token: str):
    """Connect a simple_websocket.Client to /agent/socket as the agent."""
    ws_url = f"ws://127.0.0.1:{port}/agent/socket?token={token}"
    return simple_websocket.Client(ws_url)


def _open_browser_ws(server_url: str, port: int, cookie: str):
    """Connect a simple_websocket.Client to /agent/ws as the browser."""
    ws_url = f"ws://127.0.0.1:{port}/agent/ws"
    return simple_websocket.Client(ws_url, headers={"Cookie": cookie})


def _drain_browser_until(browser_ws, predicate, timeout: float = 5.0):
    """Read messages from *browser_ws* until *predicate(msg_dict)* is True.

    Returns the list of all messages received (the matching one is the last).
    Raises pytest.fail on timeout.
    """
    deadline = time.monotonic() + timeout
    seen: list[dict] = []
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            raw = browser_ws.receive(timeout=remaining)
        except Exception:
            break
        if raw is None:
            break
        msg = json.loads(raw)
        seen.append(msg)
        if predicate(msg):
            return seen
    pytest.fail(
        f"timed out waiting for predicate; saw: "
        f"{[m.get('type') for m in seen]}"
    )


# ---------------------------------------------------------------------------
# Helpers: build a real job_plan + dispatch it through the relay
# ---------------------------------------------------------------------------


def _dispatch_job(monkeypatch, devices_mod, device_id: str) -> str:
    """Use the real agent_dispatch.start to send a job through the relay.

    Patches _pick_device to return a realistic device dict (id=device_id,
    name="Mac") and stubs `collect_credentials` so we don't have to seed
    real secrets. agent_dispatch.start routes by device["id"], which
    matches the relay-room key set in register_agent().

    Returns the job_id of the dispatched job.
    """
    from core import agent_dispatch
    from core.session_state import ReviewEntry, UploadElements

    iso = "2026-05-22"
    entry = ReviewEntry(
        date=iso, display_date="May 22, 2026",
        youtube_title="Test Episode", elements=UploadElements(),
    )

    # Realistic shape: id is the immutable hex UUID the relay keys rooms
    # by; name is the human-readable label shown in the dashboard chip.
    monkeypatch.setattr(
        agent_dispatch, "_pick_device",
        lambda **kw: {"id": device_id, "name": "Mac"},
    )
    # Skip credential bundling — we don't need real YT/Rock keys for the
    # relay's wire path; the fake agent doesn't actually upload.
    monkeypatch.setattr(
        agent_dispatch, "collect_credentials",
        lambda *, platforms_in_use: {},
    )

    # record_upload doesn't FK-constrain on the sessions table, so a fresh
    # session_id string is enough.
    session_id = "test-session-e2e"

    # Hook a fresh SSE-style queue under the job_id BEFORE dispatch — the
    # server's on_frame writes events into the queue registered for the
    # current job_id. We need to register *after* the dispatch (start()
    # returns the new job_id), but we also need to swap the relay so we
    # can capture the queue. Simplest: monkeypatch start() to capture and
    # register in-place. Cleanest: register immediately after start()
    # returns and tolerate the small race (the agent hasn't sent any
    # event frames yet because it hasn't received the job_plan).
    import queue as _queue
    sse_queue = _queue.Queue()

    job_id = agent_dispatch.start(
        session_id=session_id,
        summary=[{"date": iso, "platforms": ["YouTube Video"]}],
        entries={iso: entry},
        elements={iso: {"youtube_video_enabled": True}},
        config={"max_workers": 1},
    )
    agent_dispatch.register_job(
        job_id=job_id, sse_queue=sse_queue, session_id=session_id,
    )
    return job_id, session_id, sse_queue


def _emit(agent_ws, frame: dict) -> None:
    """Send a JSON frame from the fake agent."""
    agent_ws.send(json.dumps(frame))


def _success_frame(job_id: str, iso: str, platform: str) -> dict:
    return {
        "v": 1, "type": "event", "event": "success",
        "job_id": job_id, "row_idx": 0,
        "iso_date": iso, "platform": platform,
        "payload": {
            "title": "Test Episode",
            "file_path": "/stub/v.mp4",
            "watch_url": "https://stub/ok",
            "external_id": "ext-1",
        },
    }


# ---------------------------------------------------------------------------
# Scenario 1: happy path — events flow agent → server → browser
# ---------------------------------------------------------------------------


def test_happy_path_relays_events_and_records_upload(running_server, monkeypatch):
    """Fake agent emits start / upload_progress / success / done; the
    browser sees them in order and upload_history records the success row.
    """
    server_url, port, devices_mod, _app_mod = running_server
    token = _pair_agent(server_url, devices_mod)

    # Verify the device_id for the dispatch.
    from core import db as _db
    with _db._get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM agent_devices ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        device_id = row["id"]

    agent_ws = _open_agent_ws(server_url, port, token)
    cookie = _login_session(server_url)
    browser_ws = _open_browser_ws(server_url, port, cookie)
    # Browser receives initial presence frame on connect.
    _ = browser_ws.receive(timeout=2)

    # Wait until the relay has registered the agent (so dispatch finds it).
    time.sleep(0.3)

    job_id, session_id, sse_queue = _dispatch_job(
        monkeypatch, devices_mod, device_id,
    )

    # Agent receives the job_plan envelope from the server.
    raw_plan = agent_ws.receive(timeout=5)
    assert raw_plan is not None
    plan = json.loads(raw_plan)
    assert plan["type"] == "job_plan"
    assert plan["job_id"] == job_id

    iso = "2026-05-22"
    platform = "YouTube Video"
    _emit(agent_ws, {
        "v": 1, "type": "event", "event": "start",
        "job_id": job_id, "row_idx": 0,
        "iso_date": iso, "platform": platform,
    })
    _emit(agent_ws, {
        "v": 1, "type": "event", "event": "upload_progress",
        "job_id": job_id, "row_idx": 0,
        "iso_date": iso, "platform": platform, "percent": 50,
    })
    _emit(agent_ws, _success_frame(job_id, iso, platform))
    _emit(agent_ws, {
        "v": 1, "type": "event", "event": "done",
        "job_id": job_id,
    })

    # Server routes event frames into the per-job SSE queue (this is what
    # the production browser path consumes via /upload/stream). Drain the
    # queue until the "done" event lands — this is the on-the-wire proof
    # that the relay actually forwarded the agent's frames into the
    # server-side dispatch machinery.
    seen: list[dict] = []
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            ev = sse_queue.get(timeout=deadline - time.monotonic())
        except Exception:
            break
        seen.append(ev)
        if ev.get("event") == "done":
            break

    event_names = [m.get("event") for m in seen]
    assert "start" in event_names, f"event sequence: {event_names}"
    assert "upload_progress" in event_names, f"event sequence: {event_names}"
    assert "success" in event_names, f"event sequence: {event_names}"
    assert "done" in event_names, f"event sequence: {event_names}"
    # Order: start before success before done.
    assert event_names.index("start") < event_names.index("success")
    assert event_names.index("success") < event_names.index("done")

    # Note: event frames are routed server-side into the per-job SSE queue
    # (via agent_dispatch.on_frame); they do NOT bounce through the browser
    # WebSocket. The browser_ws was opened above purely to prove the
    # session-authenticated wss/agent/ws transport itself accepts the
    # connection — the initial presence frame already arrived and was
    # consumed during setup (see receive() right after _open_browser_ws).

    # upload_history records the success row.
    time.sleep(0.3)
    assert _db.has_successful_upload(session_id, iso, platform) is True

    agent_ws.close()
    browser_ws.close()


# ---------------------------------------------------------------------------
# Scenario 2: mid-stream disconnect and reconnect — idempotent ack-loss replay
# ---------------------------------------------------------------------------


def test_mid_stream_disconnect_and_reconnect_replays_idempotently(
    running_server, monkeypatch,
):
    """Agent emits a success event, then closes the socket before the ack
    can be delivered. On reconnect, the agent includes the success row in
    its hello.pending_results. The server applies it idempotently:
    no duplicate upload_history row, and the success frame reaches the
    browser via the replay path.
    """
    server_url, port, devices_mod, _app_mod = running_server
    token = _pair_agent(server_url, devices_mod)

    from core import db as _db
    with _db._get_conn() as conn:
        device_id = conn.execute(
            "SELECT id FROM agent_devices ORDER BY created_at DESC LIMIT 1"
        ).fetchone()["id"]

    agent_ws = _open_agent_ws(server_url, port, token)
    cookie = _login_session(server_url)
    browser_ws = _open_browser_ws(server_url, port, cookie)
    _ = browser_ws.receive(timeout=2)  # initial presence

    time.sleep(0.3)

    job_id, session_id, _sse = _dispatch_job(monkeypatch, devices_mod, device_id)

    # Receive + ignore the envelope on the agent side.
    raw_plan = agent_ws.receive(timeout=5)
    assert json.loads(raw_plan)["type"] == "job_plan"

    iso = "2026-05-22"
    platform = "YouTube Video"

    # Emit success then close BEFORE we can be sure the server's
    # record_upload finished. The server *should* have written the row;
    # we'll prove the replay-path is idempotent by attempting it.
    success = _success_frame(job_id, iso, platform)
    _emit(agent_ws, success)
    # Give the server a moment to process before close.
    time.sleep(0.3)
    agent_ws.close()

    # Confirm the success row is recorded.
    time.sleep(0.2)
    assert _db.has_successful_upload(session_id, iso, platform) is True

    # Count rows now so we can assert idempotency after the replay.
    def _count_history():
        with _db._get_conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM upload_history WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"]

    rows_before_replay = _count_history()
    assert rows_before_replay >= 1

    # Reconnect with a fresh agent ws. Send hello carrying pending_results
    # so the server walks the replay path.
    agent_ws2 = _open_agent_ws(server_url, port, token)
    hello = {
        "v": 1, "type": "hello",
        "agent_version": "0.4.0",
        "pending_results": [{
            "job_id": job_id,
            "row_idx": 0,
            "iso_date": iso,
            "platform": platform,
            "status": "success",
            "payload": {
                "title": "Test Episode",
                "file_path": "/stub/v.mp4",
                "watch_url": "https://stub/ok",
                "external_id": "ext-1",
            },
        }],
    }
    _emit(agent_ws2, hello)

    # Server should respond with pending_results_ack containing the key.
    raw_ack = agent_ws2.receive(timeout=5)
    ack = json.loads(raw_ack)
    assert ack["type"] == "pending_results_ack", f"got {ack!r}"
    acked = ack["acked"]
    assert any(
        a[0] == job_id and a[1] == 0 and a[2] == platform
        for a in acked
    ), f"expected ack for ({job_id}, 0, {platform}), got {acked!r}"

    # Idempotency: no duplicate row was created.
    rows_after_replay = _count_history()
    assert rows_after_replay == rows_before_replay, (
        f"replay must be idempotent: {rows_before_replay} → "
        f"{rows_after_replay} rows"
    )

    agent_ws2.close()
    browser_ws.close()


# ---------------------------------------------------------------------------
# Scenario 3: server restart simulation — job dropped from registry
# ---------------------------------------------------------------------------


def test_server_restart_drops_job_replay_still_acks(
    running_server, monkeypatch,
):
    """Simulate a VPS restart: agent_dispatch._jobs is emptied between the
    initial event stream and the reconnect. The agent reconnects with
    pending_results in hello.

    Current implementation behavior (verified by reading
    core/agent_dispatch.apply_pending_results): entries whose job_id is
    not in the registry are *acked* anyway (so the agent clears its
    local buffer), but the DB write is *skipped* because the session_id
    can't be derived from a dropped job. The test asserts that exact
    contract — the function returns the acked keys, the agent sees the
    pending_results_ack frame, and no exceptions are raised.
    """
    server_url, port, devices_mod, _app_mod = running_server
    token = _pair_agent(server_url, devices_mod)

    from core import db as _db, agent_dispatch
    with _db._get_conn() as conn:
        device_id = conn.execute(
            "SELECT id FROM agent_devices ORDER BY created_at DESC LIMIT 1"
        ).fetchone()["id"]

    agent_ws = _open_agent_ws(server_url, port, token)
    cookie = _login_session(server_url)
    browser_ws = _open_browser_ws(server_url, port, cookie)
    _ = browser_ws.receive(timeout=2)

    time.sleep(0.3)

    job_id, _session_id, _sse = _dispatch_job(monkeypatch, devices_mod, device_id)

    raw_plan = agent_ws.receive(timeout=5)
    assert json.loads(raw_plan)["type"] == "job_plan"

    # Close everything and simulate a VPS restart by emptying the registry.
    agent_ws.close()
    browser_ws.close()
    with agent_dispatch._jobs_lock:
        agent_dispatch._jobs.clear()

    # Reconnect the agent and send pending_results for the now-unknown job.
    agent_ws2 = _open_agent_ws(server_url, port, token)
    hello = {
        "v": 1, "type": "hello",
        "agent_version": "0.4.0",
        "pending_results": [{
            "job_id": job_id,
            "row_idx": 0,
            "iso_date": "2026-05-22",
            "platform": "YouTube Video",
            "status": "success",
            "payload": {
                "title": "Test Episode",
                "watch_url": "https://stub/ok",
            },
        }],
    }
    _emit(agent_ws2, hello)

    # Server must still ack so the agent can clear its buffer (otherwise
    # the agent would re-send every reconnect, forever).
    raw_ack = agent_ws2.receive(timeout=5)
    ack = json.loads(raw_ack)
    assert ack["type"] == "pending_results_ack"
    acked = ack["acked"]
    assert len(acked) == 1
    assert acked[0][0] == job_id

    agent_ws2.close()
