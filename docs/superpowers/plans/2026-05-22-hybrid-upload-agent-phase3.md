# Hybrid Upload Agent — Phase 3: Agent-Executed Uploads

> **Status:** Shipped on 2026-05-23 (consolidated in the `codebase-completion-pass` branch — see git history for the actual per-commit work). The `- [ ]` checkboxes below are TDD step artifacts kept as-is for reference; all steps were executed.

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** The paired local agent runs the real uploaders against local media. Server builds a per-date plan (idempotent-skip filtered, with platform credentials), sends it over the existing wss relay, ingests the same event stream the browser already renders, and persists refreshed credentials + image-use back to the encrypted store.

**Architecture:**
- Server-side `core/agent_dispatch.py` mirrors `core.upload_jobs.run_batch` inputs but emits a `job_plan` envelope through the relay; ingests `event`/`credentials_updated`/`image_used`/`pending_results` frames back.
- Agent ships `agent/dispatch.py` (control plane), `agent/run_batch.py` (copy-and-trim of run_batch), `agent/secrets_shim.py` + `agent/db_shim.py` (installed as `core.secrets_store` / `core.db` in `sys.modules` so unmodified uploaders work), and a `RemotePlaywrightSession` context manager.
- Mid-run disconnect is non-fatal: agent keeps uploading, buffers events, replays on reconnect; completed-row durability via a `pending_results` list in every reconnect's hello frame, idempotently applied server-side.
- Dashboard chip near the Upload button defaults to agent when one is online; "use web instead" link flips a flag passed as `?path=web` to `/upload`.

**Tech Stack:** Python 3.11+, `flask-sock` (already wired), `pytest`, vanilla JS for the chip.

**Spec:** `docs/superpowers/specs/2026-05-22-hybrid-upload-agent-phase3-design.md`.

**Gating:** `HYBRID_AGENT_ENABLED=true` already set on the VPS.

---

## File structure

### PR-A — server side (`feat/phase3-server-dispatch`)

**Create:**
- `core/agent_dispatch.py` — dispatcher: build envelope, send via relay, ingest events, write back to db / secrets_store / image_gatherer.
- `tests/test_agent_dispatch.py` — envelope construction, idempotent-skip filter, credentials bundling, path stripping.
- `tests/test_phase3_relay_ingest.py` — incoming frame handlers (`event`, `credentials_updated`, `image_used`, `pending_results`).

**Modify:**
- `blueprints/agent.py` — extend `/agent/socket` handler to recognize the new agent→server frame types and route them to the active job's queue (`event`) or apply them out-of-band (`credentials_updated`, `image_used`, `pending_results`).
- `app.py` — `/upload` accepts `path=agent|web` (default `web`); on `path=agent` dispatch to `core.agent_dispatch.start` instead of `core.upload_jobs.run_batch`. Behind `HYBRID_AGENT_ENABLED`.
- `static/js/dld_pipeline.js` — chip beside Upload button + path flag on POST.
- `templates/index.html` — chip markup.
- `core/devices.py` — add `most_recently_seen_online()` helper.

### PR-B — agent side (`feat/phase3-agent-runner`)

**Create:**
- `agent/secrets_shim.py` — fake `core.secrets_store` backed by an in-memory dict + tempdir; mutations queue `credentials_updated` events.
- `agent/db_shim.py` — fake `core.db`: implements `record_image_use` (queues `image_used`), everything else raises `NotImplementedError`.
- `agent/remote_session.py` — `RemotePlaywrightSession` context manager built on `secrets_shim`.
- `agent/run_batch.py` — copy-and-trim of `core.upload_jobs.run_batch`. Reads its config from the envelope; no `core.db.*` calls.
- `agent/dispatch.py` — receives `job_plan` frames; installs shims; loads credentials; resolves local paths; spawns `run_batch`; pumps events back through transport.
- `agent/tests/__init__.py`, `agent/tests/test_secrets_shim.py`, `agent/tests/test_db_shim.py`, `agent/tests/test_remote_session.py`, `agent/tests/test_run_batch.py`, `agent/tests/test_dispatch.py`.

**Modify:**
- `agent/main.py` — install shims at startup (before any uploader import); register `job_plan` handler.
- `agent/scan.py` — verify all media kinds the uploaders need are scanned (`email_thumbnails` per CLAUDE.md is the suspect); extend if gaps.

### PR-C — disconnect / reconciliation (`feat/phase3-reconcile`)

**Modify:**
- `agent/dispatch.py` — bounded in-memory event buffer; replay on reconnect.
- `agent/transport.py` — hello frame carries `pending_results: [...]`; cleared on server ack.
- `blueprints/agent.py` — accept `pending_results` in hello, idempotently apply (dedup key `(job_id, row_idx, platform)`), ack.

**Create:**
- `agent/tests/test_event_buffer.py` — buffer + replay on stub disconnect.
- `tests/test_pending_results_ingest.py` — idempotency + ack roundtrip.
- `tests/integration/test_agent_phase3_end_to_end.py` — extends `tests/integration/test_agent_end_to_end.py`: assert browser receives identical event stream from the agent path vs the web-only path.

**Message envelope:** continues using the existing `{"v":1,"type":"...","payload":{...}}` shape from Phase 1. All new frame types use `"v":1`.

---

# PR-A — Server-side dispatcher + UI chip

### Task A1: `most_recently_seen_online` device helper

**Files:**
- Modify: `core/devices.py`
- Test: `tests/test_devices.py` (extend existing file if present; otherwise create)

- [ ] **Step 1: Failing test**

```python
# tests/test_devices.py
def test_most_recently_seen_online_picks_highest_last_seen(tmp_db):
    # Two devices, both online (last_seen within freshness window),
    # the one with the later last_seen wins.
    from core import devices
    devices.register("dev-a", "tokA-hash"); devices.touch_seen("dev-a", ts=100)
    devices.register("dev-b", "tokB-hash"); devices.touch_seen("dev-b", ts=200)
    assert devices.most_recently_seen_online(freshness_seconds=300, now=250).name == "dev-b"

def test_most_recently_seen_online_returns_none_when_all_stale(tmp_db):
    from core import devices
    devices.register("dev-a", "tokA-hash"); devices.touch_seen("dev-a", ts=10)
    assert devices.most_recently_seen_online(freshness_seconds=60, now=1000) is None
```

- [ ] **Step 2: Run test, expect FAIL** (`most_recently_seen_online` doesn't exist).

```bash
pytest tests/test_devices.py::test_most_recently_seen_online_picks_highest_last_seen -v
```

- [ ] **Step 3: Implement**

```python
# core/devices.py — append at the end
def most_recently_seen_online(freshness_seconds: int = 60, now: float | None = None):
    """Return the device row whose last_seen_at is the largest among
    non-revoked devices, provided it's within `freshness_seconds` of `now`.
    None if no device qualifies."""
    import time as _t
    cutoff = (now if now is not None else _t.time()) - freshness_seconds
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM agent_devices "
            "WHERE revoked = 0 AND last_seen_at >= ? "
            "ORDER BY last_seen_at DESC LIMIT 1",
            (cutoff,),
        ).fetchone()
    return _row_to_obj(row) if row else None
```

- [ ] **Step 4: Run test, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add core/devices.py tests/test_devices.py
git commit -m "feat(devices): most_recently_seen_online() helper for agent dispatch"
```

### Task A2: `agent_dispatch.build_envelope` — envelope shape

**Files:**
- Create: `core/agent_dispatch.py`
- Test: `tests/test_agent_dispatch.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_agent_dispatch.py
import json
from core import agent_dispatch
from core.session_state import ReviewEntry

def _entry(date="2026-05-22"):
    e = ReviewEntry(date=date)
    e.youtube_title = "T"; e.media_path = "/server/tmp/v.mp4"
    e.thumbnail_path = "/server/tmp/th.png"
    return e

def test_build_envelope_strips_path_fields_from_entries():
    entries = {"2026-05-22": _entry()}
    summary = [{"date": "2026-05-22", "platforms": ["YouTube Video"]}]
    elements = {"youtube_video_enabled": True}
    env = agent_dispatch.build_envelope(
        job_id="J1",
        rows=[{"row_idx": 0, "iso_date": "2026-05-22",
               "platforms": ["YouTube Video"], "elements": elements}],
        entries=entries,
        credentials={"youtube.token": "{}"},
        config={"max_workers": 4},
    )
    assert env["type"] == "job_plan"
    assert env["job_id"] == "J1"
    assert env["protocol_version"] == 1
    assert env["rows"][0]["entry"]["youtube_title"] == "T"
    assert "media_path" not in env["rows"][0]["entry"]
    assert "thumbnail_path" not in env["rows"][0]["entry"]
    assert env["credentials"] == {"youtube.token": "{}"}
    assert json.dumps(env)  # round-trips as JSON
```

- [ ] **Step 2: Run test, expect FAIL** (module missing).

- [ ] **Step 3: Implement**

```python
# core/agent_dispatch.py
"""Server-side dispatcher: builds a job_plan envelope for the agent path,
sends it through the relay, and ingests the result stream.

Mirrors core.upload_jobs.run_batch inputs but never calls uploaders
locally — execution happens on the paired agent. See
docs/superpowers/specs/2026-05-22-hybrid-upload-agent-phase3-design.md.
"""
from __future__ import annotations
import logging
from typing import Any

_PROTOCOL_VERSION = 1

# Path fields removed from a serialized ReviewEntry before send; the agent
# re-resolves them from its own scan map.
_STRIPPED_PATH_FIELDS = (
    "media_path",
    "thumbnail_path",
    "email_thumbnail_path",
    "spotlight_image_path",
    "vista_image_path",
    "reflection_image_path",
)

_logger = logging.getLogger(__name__)


def _strip_paths(entry_dict: dict) -> dict:
    return {k: v for k, v in entry_dict.items() if k not in _STRIPPED_PATH_FIELDS}


def build_envelope(
    *,
    job_id: str,
    rows: list[dict],
    entries: dict,        # iso_date -> ReviewEntry
    credentials: dict,    # secrets_store key -> blob string
    config: dict,
) -> dict:
    """Compose the job_plan envelope. Pure function; no I/O."""
    out_rows = []
    for r in rows:
        iso = r["iso_date"]
        entry = entries[iso]
        out_rows.append({
            "row_idx": r["row_idx"],
            "iso_date": iso,
            "platforms": list(r["platforms"]),
            "elements": r["elements"],
            "entry": _strip_paths(entry.to_dict()),
        })
    return {
        "v": 1,
        "type": "job_plan",
        "job_id": job_id,
        "protocol_version": _PROTOCOL_VERSION,
        "config": config,
        "rows": out_rows,
        "credentials": dict(credentials),
    }
```

- [ ] **Step 4: Run test, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add core/agent_dispatch.py tests/test_agent_dispatch.py
git commit -m "feat(agent_dispatch): build_envelope (pure construction with path stripping)"
```

### Task A3: Idempotent-skip filter

**Files:**
- Modify: `core/agent_dispatch.py`
- Test: `tests/test_agent_dispatch.py`

- [ ] **Step 1: Failing test**

```python
def test_filter_already_done_rows_drops_completed_platforms(tmp_db, monkeypatch):
    from core import agent_dispatch, db as _db
    _db.record_upload(session_id="S1", iso_date="2026-05-22",
                      platform="YouTube Video", status="success", payload={})
    summary = [
        {"date": "2026-05-22", "platforms": ["YouTube Video", "Rock"]},
        {"date": "2026-05-23", "platforms": ["YouTube Video"]},
    ]
    rows = agent_dispatch.filter_done_rows(session_id="S1", summary=summary)
    # YouTube Video on 05-22 is done — dropped. Rock on 05-22 + YouTube on 05-23 remain.
    assert rows == [
        {"row_idx": 0, "iso_date": "2026-05-22", "platforms": ["Rock"]},
        {"row_idx": 1, "iso_date": "2026-05-23", "platforms": ["YouTube Video"]},
    ]

def test_filter_drops_row_entirely_when_all_platforms_done(tmp_db):
    from core import agent_dispatch, db as _db
    _db.record_upload(session_id="S1", iso_date="2026-05-22",
                      platform="YouTube Video", status="success", payload={})
    summary = [{"date": "2026-05-22", "platforms": ["YouTube Video"]}]
    assert agent_dispatch.filter_done_rows(session_id="S1", summary=summary) == []
```

- [ ] **Step 2: Run test, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# core/agent_dispatch.py — append
from core import db as _db


def filter_done_rows(*, session_id: str, summary: list[dict]) -> list[dict]:
    """Drop platforms (and entire rows) already recorded as `success`
    in upload_history. Output is a list of {row_idx, iso_date, platforms}
    in the same row order as `summary`, with done platforms removed."""
    out: list[dict] = []
    for idx, item in enumerate(summary):
        iso = item["date"]
        remaining = [
            p for p in item["platforms"]
            if not _db.has_successful_upload(session_id, iso, p)
        ]
        if remaining:
            out.append({"row_idx": idx, "iso_date": iso, "platforms": remaining})
    return out
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add core/agent_dispatch.py tests/test_agent_dispatch.py
git commit -m "feat(agent_dispatch): filter_done_rows applies idempotent-skip server-side"
```

### Task A4: Credentials bundling from `secrets_store`

**Files:**
- Modify: `core/agent_dispatch.py`
- Test: `tests/test_agent_dispatch.py`

- [ ] **Step 1: Failing test**

```python
def test_collect_credentials_pulls_needed_keys_only(tmp_secrets):
    from core import agent_dispatch, secrets_store
    secrets_store.set_secret("youtube.token", '{"t":1}')
    secrets_store.set_secret("youtube.client_secrets", '{"c":1}')
    secrets_store.set_secret("rock.session", '{"r":1}')
    secrets_store.set_secret("simplecast.session", '{"s":1}')
    secrets_store.set_secret("vista_social.session", '{"v":1}')
    creds = agent_dispatch.collect_credentials(
        platforms_in_use={"YouTube Video", "Rock"},
    )
    # Only the keys actually needed for selected platforms come through.
    assert set(creds.keys()) == {
        "youtube.token", "youtube.client_secrets", "rock.session",
    }
    assert creds["youtube.token"] == '{"t":1}'

def test_collect_credentials_omits_missing_keys(tmp_secrets):
    from core import agent_dispatch
    # Nothing in store.
    assert agent_dispatch.collect_credentials(platforms_in_use={"Rock"}) == {}
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# core/agent_dispatch.py — append
from core import secrets_store as _ss

# Which secrets_store keys each platform name requires. Keys absent from
# the store are simply omitted from the envelope (uploaders may surface
# a clearer error than a None on the agent side if missing).
_PLATFORM_KEYS: dict[str, tuple[str, ...]] = {
    "YouTube Video":   ("youtube.token", "youtube.client_secrets"),
    "YouTube Shorts":  ("youtube.token", "youtube.client_secrets"),
    "Rock":            ("rock.session",),
    "Rock Email":      ("rock.session",),
    "Simplecast":      ("simplecast.session",),
    "Vista Social":    ("vista_social.session",),
}


def collect_credentials(*, platforms_in_use: set[str]) -> dict[str, str]:
    """Return only the secrets_store entries needed for the given platforms.
    Missing keys are silently omitted."""
    needed: set[str] = set()
    for p in platforms_in_use:
        needed.update(_PLATFORM_KEYS.get(p, ()))
    out: dict[str, str] = {}
    for key in sorted(needed):
        val = _ss.get_secret(key)
        if val is not None:
            out[key] = val
    return out
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add core/agent_dispatch.py tests/test_agent_dispatch.py
git commit -m "feat(agent_dispatch): collect_credentials bundles platform secrets"
```

### Task A5: `start()` — wire filter + envelope + relay send

**Files:**
- Modify: `core/agent_dispatch.py`
- Test: `tests/test_agent_dispatch.py`

- [ ] **Step 1: Failing test**

```python
def test_start_sends_envelope_through_relay_and_returns_job_id(monkeypatch, tmp_db, tmp_secrets):
    from core import agent_dispatch, secrets_store, relay
    secrets_store.set_secret("youtube.token", "{}")
    sent: list = []
    monkeypatch.setattr(relay, "send_to_device",
                        lambda device_name, envelope: sent.append((device_name, envelope)))
    monkeypatch.setattr(agent_dispatch, "_pick_device",
                        lambda: type("D", (), {"name": "mac-1"})())

    job_id = agent_dispatch.start(
        session_id="S1",
        summary=[{"date": "2026-05-22", "platforms": ["YouTube Video"]}],
        entries={"2026-05-22": _entry()},
        elements={"youtube_video_enabled": True},
        config={"max_workers": 4},
    )
    assert isinstance(job_id, str) and len(job_id) > 0
    assert len(sent) == 1
    device, env = sent[0]
    assert device == "mac-1"
    assert env["type"] == "job_plan"
    assert env["job_id"] == job_id
    assert env["rows"][0]["iso_date"] == "2026-05-22"
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# core/agent_dispatch.py — append
import uuid as _uuid
from core import devices as _devices, relay as _relay


class NoAgentOnlineError(RuntimeError):
    """Raised when /upload?path=agent is invoked but no paired agent is online."""


def _pick_device():
    dev = _devices.most_recently_seen_online()
    if dev is None:
        raise NoAgentOnlineError("no paired agent is online")
    return dev


def start(
    *,
    session_id: str,
    summary: list[dict],
    entries: dict,
    elements: dict,
    config: dict,
) -> str:
    """Filter done rows, bundle credentials, build the envelope, and send
    it through the relay to the chosen agent. Returns the new job_id."""
    job_id = _uuid.uuid4().hex
    rows = filter_done_rows(session_id=session_id, summary=summary)
    if not rows:
        _logger.info("agent_dispatch.start(job=%s): nothing to do", job_id)
        return job_id
    for r in rows:
        r["elements"] = elements
    platforms_in_use: set[str] = set()
    for r in rows:
        platforms_in_use.update(r["platforms"])
    creds = collect_credentials(platforms_in_use=platforms_in_use)
    envelope = build_envelope(
        job_id=job_id,
        rows=rows,
        entries=entries,
        credentials=creds,
        config=config,
    )
    device = _pick_device()
    _relay.send_to_device(device.name, envelope)
    _logger.info("agent_dispatch.start(job=%s, device=%s, rows=%d)",
                 job_id, device.name, len(rows))
    return job_id
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add core/agent_dispatch.py tests/test_agent_dispatch.py
git commit -m "feat(agent_dispatch): start() filter+bundle+send via relay"
```

### Task A6: Relay-frame ingest — `event` → SSE queue

**Files:**
- Modify: `core/agent_dispatch.py` (registry of active jobs + `on_frame`)
- Modify: `blueprints/agent.py` (forward incoming frames to `on_frame`)
- Test: `tests/test_phase3_relay_ingest.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_phase3_relay_ingest.py
import queue
from core import agent_dispatch

def test_event_frame_routed_to_job_queue():
    q = queue.Queue()
    agent_dispatch.register_job(job_id="J1", sse_queue=q)
    agent_dispatch.on_frame({"v":1, "type": "event", "job_id": "J1",
                             "row_idx": 0, "event": "upload_progress",
                             "platform": "YouTube Video", "percent": 42})
    msg = q.get_nowait()
    assert msg["event"] == "upload_progress"
    assert msg["row_idx"] == 0
    assert msg["percent"] == 42

def test_event_for_unknown_job_is_dropped_without_error():
    agent_dispatch.on_frame({"v":1, "type": "event", "job_id": "missing",
                             "row_idx": 0, "event": "start"})
    # No exception; nothing to assert beyond that.
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# core/agent_dispatch.py — append
import threading as _threading

_jobs: dict[str, dict] = {}
_jobs_lock = _threading.RLock()


def register_job(*, job_id: str, sse_queue) -> None:
    with _jobs_lock:
        _jobs[job_id] = {"queue": sse_queue}


def drop_job(job_id: str) -> None:
    with _jobs_lock:
        _jobs.pop(job_id, None)


def _job(job_id: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def on_frame(frame: dict) -> None:
    """Dispatch one agent->server frame. Safe to call from the relay
    socket thread."""
    ftype = frame.get("type")
    if ftype == "event":
        job = _job(frame.get("job_id", ""))
        if job is None:
            _logger.debug("event for unknown job %s dropped", frame.get("job_id"))
            return
        job["queue"].put({k: v for k, v in frame.items() if k not in ("v", "type", "job_id")})
        return
    # credentials_updated / image_used / pending_results handled in later tasks.
    _logger.debug("agent_dispatch.on_frame: unhandled type %r", ftype)
```

- [ ] **Step 4: Wire `blueprints/agent.py` to forward incoming frames.**

In `blueprints/agent.py`, find `agent_socket(ws)` and add inside the message-receive loop, after the existing `ping`/`scan_result` handling:

```python
            elif mtype in ("event", "credentials_updated", "image_used", "pending_results_chunk"):
                # Phase 3: route agent->server upload traffic to the
                # dispatch ingestor. Out-of-band frames (creds, image-use,
                # pending results) are applied by agent_dispatch.on_frame.
                from core import agent_dispatch
                try:
                    agent_dispatch.on_frame(msg)
                except Exception as e:
                    log.warning("agent_dispatch.on_frame failed: %s", e)
```

(Use the existing `mtype = msg.get("type")` variable name from that file.)

- [ ] **Step 5: Run, expect PASS.**

- [ ] **Step 6: Commit**

```bash
git add core/agent_dispatch.py blueprints/agent.py tests/test_phase3_relay_ingest.py
git commit -m "feat(agent_dispatch): on_frame routes event frames to SSE queue"
```

### Task A7: `success` events write to `upload_history`

**Files:**
- Modify: `core/agent_dispatch.py`
- Test: `tests/test_phase3_relay_ingest.py`

- [ ] **Step 1: Failing test**

```python
def test_success_event_records_upload_history(tmp_db):
    import queue
    from core import agent_dispatch, db as _db
    q = queue.Queue()
    agent_dispatch.register_job(job_id="J2", sse_queue=q,
                                session_id="S1")
    agent_dispatch.on_frame({
        "v":1, "type": "event", "job_id": "J2", "row_idx": 0,
        "event": "success", "platform": "YouTube Video",
        "iso_date": "2026-05-22", "payload": {"watch_url": "https://yt/x"},
    })
    assert _db.has_successful_upload("S1", "2026-05-22", "YouTube Video") is True
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement** — extend `register_job` to take `session_id`, and dispatch on event name:

```python
# core/agent_dispatch.py — replace existing register_job + extend on_frame
def register_job(*, job_id: str, sse_queue, session_id: str | None = None) -> None:
    with _jobs_lock:
        _jobs[job_id] = {"queue": sse_queue, "session_id": session_id}
```

Then in `on_frame`, where you handle `"event"`, add a branch:

```python
        # before the queue.put:
        if frame.get("event") == "success" and job.get("session_id"):
            try:
                _db.record_upload(
                    session_id=job["session_id"],
                    iso_date=frame.get("iso_date", ""),
                    platform=frame.get("platform", ""),
                    status="success",
                    payload=frame.get("payload") or {},
                )
            except Exception as e:
                _logger.warning("record_upload failed: %s", e)
```

- [ ] **Step 4: Update Task A6 test** (`test_event_frame_routed_to_job_queue`) — pass `session_id=None` to `register_job` so it stays opt-in. Run all tests in the file, expect PASS.

- [ ] **Step 5: Commit**

```bash
git add core/agent_dispatch.py tests/test_phase3_relay_ingest.py
git commit -m "feat(agent_dispatch): on success event, record upload_history"
```

### Task A8: `credentials_updated` writes back to `secrets_store`

**Files:**
- Modify: `core/agent_dispatch.py`
- Test: `tests/test_phase3_relay_ingest.py`

- [ ] **Step 1: Failing test**

```python
def test_credentials_updated_writes_back_to_secrets_store(tmp_secrets):
    from core import agent_dispatch, secrets_store
    agent_dispatch.on_frame({
        "v":1, "type": "credentials_updated", "job_id": "J3",
        "key": "youtube.token", "value": '{"refreshed": true}',
    })
    assert secrets_store.get_secret("youtube.token") == '{"refreshed": true}'
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement** — extend `on_frame`:

```python
# core/agent_dispatch.py — inside on_frame, add a branch:
    elif ftype == "credentials_updated":
        key, value = frame.get("key"), frame.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            _logger.warning("credentials_updated: bad shape %r", frame)
            return
        try:
            _ss.set_secret(key, value)
        except Exception as e:
            _logger.warning("secrets_store.set_secret(%s) failed: %s", key, e)
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add core/agent_dispatch.py tests/test_phase3_relay_ingest.py
git commit -m "feat(agent_dispatch): credentials_updated writes back to secrets_store"
```

### Task A9: `image_used` records via `db` + `image_gatherer`

**Files:**
- Modify: `core/agent_dispatch.py`
- Test: `tests/test_phase3_relay_ingest.py`

- [ ] **Step 1: Failing test**

```python
def test_image_used_records_db_and_credits(tmp_db, tmp_credits_file, monkeypatch):
    from core import agent_dispatch, db as _db, image_gatherer
    appended: list = []
    monkeypatch.setattr(image_gatherer, "append_credits_entry",
                        lambda **kw: appended.append(kw))
    agent_dispatch.on_frame({
        "v":1, "type": "image_used", "job_id": "J4", "row_idx": 0,
        "photo_id": "ph-1", "source": "unsplash", "topic": "joy",
        "used_on_date": "2026-05-22", "photographer": "Jane",
        "photo_url": "https://u/p1",
    })
    # db row exists
    assert _db.image_was_used("ph-1") is True  # add this helper or inline a fetch
    # credits append called once
    assert len(appended) == 1
    assert appended[0]["used_on_date"] == "2026-05-22"
```

If `image_was_used` doesn't exist, use a direct cursor in the test or skip that part and only assert `appended`.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement** — extend `on_frame`:

```python
# core/agent_dispatch.py
from core import image_gatherer as _img

# inside on_frame:
    elif ftype == "image_used":
        try:
            _db.record_image_use(
                photo_id=frame["photo_id"],
                source=frame["source"],
                topic=frame["topic"],
                used_on_date=frame["used_on_date"],
                photographer=frame.get("photographer"),
                photo_url=frame.get("photo_url"),
            )
        except Exception as e:
            _logger.warning("record_image_use failed: %s", e)
        try:
            _img.append_credits_entry(
                used_on_date=frame["used_on_date"],
                photographer=frame.get("photographer"),
                photo_url=frame.get("photo_url"),
                topic=frame.get("topic"),
                source=frame.get("source"),
            )
        except Exception as e:
            _logger.warning("append_credits_entry failed: %s", e)
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add core/agent_dispatch.py tests/test_phase3_relay_ingest.py
git commit -m "feat(agent_dispatch): image_used records db + credits append"
```

### Task A10: `/upload` accepts `path=agent` flag

**Files:**
- Modify: `app.py`
- Test: `tests/test_upload_route_path_flag.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_upload_route_path_flag.py
def test_upload_path_agent_dispatches_to_agent_dispatch(client, monkeypatch, app_with_hybrid_enabled):
    from core import agent_dispatch
    called: dict = {}
    def _fake_start(**kw):
        called.update(kw); return "JX"
    monkeypatch.setattr(agent_dispatch, "start", _fake_start)
    r = client.post("/upload?path=agent", json={"some": "payload"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["job_id"] == "JX"
    assert called  # agent_dispatch.start invoked

def test_upload_path_web_keeps_running_run_batch(client, monkeypatch):
    from core import upload_jobs
    called = {"run_batch": False}
    monkeypatch.setattr(upload_jobs, "run_batch",
                        lambda **kw: called.__setitem__("run_batch", True) or set())
    r = client.post("/upload", json={"some": "payload"})
    assert r.status_code == 200
    assert called["run_batch"] is True

def test_upload_path_agent_with_flag_off_falls_through_to_web(client, monkeypatch, app_without_hybrid):
    from core import agent_dispatch, upload_jobs
    monkeypatch.setattr(agent_dispatch, "start", lambda **kw: pytest.fail("should not run"))
    called = {"run_batch": False}
    monkeypatch.setattr(upload_jobs, "run_batch",
                        lambda **kw: called.__setitem__("run_batch", True) or set())
    r = client.post("/upload?path=agent", json={"some": "payload"})
    assert called["run_batch"] is True
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement** — in `app.py`, in the `/upload` route handler, before the existing `upload_jobs.run_batch` call:

```python
    # Phase 3: route to the local agent if the dashboard chose that path.
    use_agent = (
        request.args.get("path") == "agent"
        and os.environ.get("HYBRID_AGENT_ENABLED", "").lower() == "true"
    )
    if use_agent:
        from core import agent_dispatch
        try:
            job_id = agent_dispatch.start(
                session_id=session_id,
                summary=summary,
                entries=entries_snapshot,
                elements=session.elements_for_run(),
                config={"max_workers": _cfg("upload.max_workers", 4)},
            )
        except agent_dispatch.NoAgentOnlineError:
            return jsonify({"error": "no_agent_online"}), 409
        sse_q = _job_queues.setdefault(job_id, queue.Queue())
        agent_dispatch.register_job(job_id=job_id, sse_queue=sse_q,
                                     session_id=session_id)
        return jsonify({"job_id": job_id})
```

(Wire up names — `_job_queues`, `_cfg`, `session.elements_for_run()` etc. — using whatever the existing `/upload` handler uses.)

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_upload_route_path_flag.py
git commit -m "feat(upload): /upload?path=agent dispatches via agent_dispatch (gated)"
```

### Task A11: Dashboard chip + path flag in JS

**Files:**
- Modify: `templates/index.html`, `static/js/dld_pipeline.js`
- Test: smoke-only — manual page load. (UI test infra isn't worth scaffolding for two DOM elements.)

- [ ] **Step 1: Add chip markup** to `templates/index.html`, immediately before the Upload button:

```html
<span id="agent-chip" class="agent-chip" hidden>
  <span class="agent-chip__label">via agent: <span id="agent-chip-name">…</span></span>
  <a href="#" class="agent-chip__toggle" id="agent-chip-toggle">use web instead</a>
</span>
```

- [ ] **Step 2: Minimal CSS** in `static/css/dld.css` (or wherever pipeline styles live):

```css
.agent-chip { display: inline-flex; gap: .5em; padding: .25em .5em;
              border-radius: 999px; background: #e8f4ff; font-size: .85em;
              align-items: center; }
.agent-chip__toggle { font-size: .75em; opacity: .7; }
.agent-chip[data-path="web"] { background: #f0f0f0; }
.agent-chip[data-path="web"] .agent-chip__label { text-decoration: line-through; opacity: .6; }
```

- [ ] **Step 3: JS — show chip on agent-online presence, flip on toggle, append path flag.**

In `static/js/dld_pipeline.js`, add:

```javascript
// Phase 3: agent path chip
const _agentState = { online: false, deviceName: null, chosenPath: "agent" };

function _updateAgentChip() {
  const chip = document.getElementById("agent-chip");
  const name = document.getElementById("agent-chip-name");
  if (!chip || !name) return;
  if (_agentState.online) {
    name.textContent = _agentState.deviceName || "device";
    chip.hidden = false;
    chip.dataset.path = _agentState.chosenPath;
  } else {
    chip.hidden = true;
    _agentState.chosenPath = "web";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.getElementById("agent-chip-toggle");
  if (toggle) {
    toggle.addEventListener("click", (e) => {
      e.preventDefault();
      _agentState.chosenPath = _agentState.chosenPath === "agent" ? "web" : "agent";
      toggle.textContent = _agentState.chosenPath === "agent"
        ? "use web instead" : "use agent instead";
      _updateAgentChip();
    });
  }
});

// Called by the existing browser-socket presence handler:
function onAgentPresence(presence) {
  _agentState.online = !!presence.online;
  _agentState.deviceName = presence.device_name || null;
  _updateAgentChip();
}

// Wherever the upload POST is sent, append ?path=
function _uploadPath() {
  return _agentState.online && _agentState.chosenPath === "agent" ? "agent" : "web";
}
```

Find the existing `fetch("/upload", ...)` call and replace with
`fetch("/upload?path=" + _uploadPath(), ...)`.

Wire `onAgentPresence` into wherever the existing `/agent/ws` browser-socket presence frames are handled — the simplest path is to find the message dispatch in this file and add `if (msg.type === "agent_presence") onAgentPresence(msg);`.

- [ ] **Step 4: Manual smoke**

```bash
flask run
# Browse http://localhost:8080; with HYBRID_AGENT_ENABLED unset, chip stays hidden.
# With agent paired and online, chip appears.
```

- [ ] **Step 5: Commit**

```bash
git add templates/index.html static/js/dld_pipeline.js static/css/dld.css
git commit -m "feat(ui): agent chip near Upload, defaults to agent, web override"
```

### Task A12: PR-A finishing commit

- [ ] **Step 1: Run the full test suite**

```bash
pytest -q
```

- [ ] **Step 2: Push the branch and open PR-A**

```bash
git push -u origin feat/phase3-server-dispatch
gh pr create --base main --title "Phase 3 PR-A: server-side agent_dispatch + UI chip" \
  --body "Implements the server side of Phase 3 of the hybrid upload agent.

See docs/superpowers/specs/2026-05-22-hybrid-upload-agent-phase3-design.md.

Adds core/agent_dispatch.py: envelope construction, idempotent-skip filter,
credentials bundling, relay send, and on_frame ingest for event /
credentials_updated / image_used.

/upload?path=agent routes through agent_dispatch when HYBRID_AGENT_ENABLED.
Otherwise the web-only path runs unchanged.

Dashboard chip beside Upload shows the chosen path and lets the user fall
back to web."
```

---

# PR-B — Agent-side runner + shims

### Task B1: `agent/secrets_shim.py` — get/set/delete + materialize

**Files:**
- Create: `agent/secrets_shim.py`
- Test: `agent/tests/__init__.py`, `agent/tests/test_secrets_shim.py`

- [ ] **Step 1: `agent/tests/__init__.py`**: empty file so pytest discovers.

- [ ] **Step 2: Failing test**

```python
# agent/tests/test_secrets_shim.py
import json, os, tempfile
import pytest
from agent import secrets_shim

def test_get_returns_value_seeded_from_envelope():
    s = secrets_shim.Shim(initial={"a.k": "v1"})
    assert s.get_secret("a.k") == "v1"
    assert s.get_secret("missing") is None

def test_set_emits_credentials_updated_and_overwrites():
    emitted = []
    s = secrets_shim.Shim(initial={}, emit=emitted.append)
    s.set_secret("youtube.token", "{}")
    s.set_secret("youtube.token", "{\"refreshed\":1}")
    assert s.get_secret("youtube.token") == "{\"refreshed\":1}"
    assert [e["key"] for e in emitted] == ["youtube.token", "youtube.token"]
    assert emitted[-1]["value"] == "{\"refreshed\":1}"
    assert emitted[-1]["type"] == "credentials_updated"

def test_delete_emits_credentials_updated_with_empty_value():
    emitted = []
    s = secrets_shim.Shim(initial={"k": "v"}, emit=emitted.append)
    s.delete_secret("k")
    assert s.get_secret("k") is None
    assert emitted[-1] == {"type": "credentials_updated", "key": "k", "value": ""}

def test_materialize_writes_to_tempfile_and_cleans_up():
    s = secrets_shim.Shim(initial={"yt.cs": '{"client":"x"}'})
    with s.materialize_blob_to_tempfile("yt.cs", suffix=".json") as path:
        assert path and os.path.exists(path)
        assert json.load(open(path)) == {"client": "x"}
        held = path
    assert not os.path.exists(held)

def test_materialize_returns_none_when_missing():
    s = secrets_shim.Shim(initial={})
    with s.materialize_blob_to_tempfile("missing.k") as path:
        assert path is None
```

- [ ] **Step 3: Run, expect FAIL.**

- [ ] **Step 4: Implement**

```python
# agent/secrets_shim.py
"""Drop-in replacement for `core.secrets_store` on the agent.

Installed into sys.modules as 'core.secrets_store' at agent startup so
bundled uploaders (notably uploaders/youtube_uploader.py) work unchanged.
Backed by an in-memory dict + per-call tempfiles; mutations are emitted
back to the server as `credentials_updated` events. The server is the
source of truth — the shim never touches a SQLite DB or master key.
"""
from __future__ import annotations
import contextlib
import os
import tempfile
from typing import Callable, Optional, Iterator

_EmitFn = Callable[[dict], None]


class Shim:
    def __init__(self, *, initial: Optional[dict] = None,
                 emit: Optional[_EmitFn] = None) -> None:
        self._d: dict[str, str] = dict(initial or {})
        self._emit: _EmitFn = emit or (lambda _frame: None)

    def get_secret(self, key: str) -> Optional[str]:
        return self._d.get(key)

    def set_secret(self, key: str, value: str) -> None:
        self._d[key] = value
        self._emit({"type": "credentials_updated", "key": key, "value": value})

    def delete_secret(self, key: str) -> None:
        self._d.pop(key, None)
        self._emit({"type": "credentials_updated", "key": key, "value": ""})

    @contextlib.contextmanager
    def materialize_blob_to_tempfile(self, key: str, *,
                                     suffix: str = "") -> Iterator[Optional[str]]:
        val = self._d.get(key)
        if val is None:
            yield None
            return
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="dld-cred-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(val)
            yield path
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


# Default singleton used when the shim is installed module-level.
_default = Shim()


def install_as_core_secrets_store(*, initial: dict, emit: _EmitFn) -> Shim:
    """Replace core.secrets_store in sys.modules with module-level functions
    that delegate to a fresh Shim. Returns the new Shim so the dispatch
    layer can keep a handle for swapping `initial` between jobs."""
    import sys as _sys, types as _types
    shim = Shim(initial=initial, emit=emit)
    mod = _types.ModuleType("core.secrets_store")
    mod.get_secret = shim.get_secret           # type: ignore[attr-defined]
    mod.set_secret = shim.set_secret           # type: ignore[attr-defined]
    mod.delete_secret = shim.delete_secret     # type: ignore[attr-defined]
    mod.materialize_blob_to_tempfile = shim.materialize_blob_to_tempfile  # type: ignore[attr-defined]
    _sys.modules["core.secrets_store"] = mod
    return shim
```

- [ ] **Step 5: Run, expect PASS.**

- [ ] **Step 6: Commit**

```bash
git add agent/secrets_shim.py agent/tests/__init__.py agent/tests/test_secrets_shim.py
git commit -m "feat(agent): secrets_shim — drop-in core.secrets_store backed by envelope"
```

### Task B2: `agent/db_shim.py` — record_image_use + NotImplementedError fallback

**Files:**
- Create: `agent/db_shim.py`
- Test: `agent/tests/test_db_shim.py`

- [ ] **Step 1: Failing test**

```python
# agent/tests/test_db_shim.py
import pytest
from agent import db_shim

def test_record_image_use_emits_image_used():
    emitted = []
    shim = db_shim.Shim(emit=emitted.append)
    shim.record_image_use(photo_id="p1", source="unsplash", topic="joy",
                          used_on_date="2026-05-22",
                          photographer="Jane", photo_url="https://u/p1")
    assert emitted == [{
        "type": "image_used",
        "photo_id": "p1", "source": "unsplash", "topic": "joy",
        "used_on_date": "2026-05-22",
        "photographer": "Jane", "photo_url": "https://u/p1",
    }]

def test_any_other_attr_raises_not_implemented():
    shim = db_shim.Shim(emit=lambda _f: None)
    with pytest.raises(NotImplementedError) as e:
        shim.has_successful_upload("S1", "2026-05-22", "Rock")
    assert "agent does not implement" in str(e.value)
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# agent/db_shim.py
"""Drop-in replacement for `core.db` on the agent.

Implements only the calls bundled uploaders make at runtime
(currently: record_image_use from uploaders/rock/orchestrator.py).
Every other db.* attribute access raises NotImplementedError so
future coupling surfaces loudly instead of silently failing on a
SQLite file the agent doesn't have.
"""
from __future__ import annotations
from typing import Callable

_EmitFn = Callable[[dict], None]


class Shim:
    def __init__(self, *, emit: _EmitFn) -> None:
        self._emit = emit

    def record_image_use(self, *, photo_id, source, topic, used_on_date,
                         photographer=None, photo_url=None) -> None:
        self._emit({
            "type": "image_used",
            "photo_id": photo_id,
            "source": source,
            "topic": topic,
            "used_on_date": used_on_date,
            "photographer": photographer,
            "photo_url": photo_url,
        })

    def __getattr__(self, name: str):
        raise NotImplementedError(
            f"agent does not implement core.db.{name} — the agent ships a "
            "minimal db_shim. Add the call to agent/db_shim.py if you really "
            "need it on the agent path."
        )


def install_as_core_db(*, emit: _EmitFn) -> Shim:
    import sys as _sys, types as _types
    shim = Shim(emit=emit)
    mod = _types.ModuleType("core.db")
    # Bind the methods we DO support as module-level callables.
    mod.record_image_use = shim.record_image_use   # type: ignore[attr-defined]
    # Sentinel that proxies all other attrs to NotImplementedError.
    def _missing(name):
        def _raise(*a, **kw):
            raise NotImplementedError(
                f"agent does not implement core.db.{name} — see agent/db_shim.py"
            )
        return _raise
    class _ProxyModule(_types.ModuleType):
        def __getattr__(self, name):
            return _missing(name)
    proxy = _ProxyModule("core.db")
    proxy.record_image_use = shim.record_image_use  # type: ignore[attr-defined]
    _sys.modules["core.db"] = proxy
    return shim
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add agent/db_shim.py agent/tests/test_db_shim.py
git commit -m "feat(agent): db_shim — record_image_use stub; raise on other db.* calls"
```

### Task B3: `agent/remote_session.py` — `RemotePlaywrightSession`

**Files:**
- Create: `agent/remote_session.py`
- Test: `agent/tests/test_remote_session.py`

- [ ] **Step 1: Failing test**

```python
# agent/tests/test_remote_session.py
import os, json
from agent.remote_session import RemotePlaywrightSession
from agent.secrets_shim import Shim

def test_enter_writes_blob_to_tempfile_and_exit_cleans_up():
    s = Shim(initial={"rock.session": '{"cookies":[]}'})
    with RemotePlaywrightSession(s, "rock.session") as path:
        assert os.path.exists(path)
        assert json.load(open(path)) == {"cookies": []}
        held = path
    assert not os.path.exists(held)

def test_exit_emits_credentials_updated_when_contents_change():
    emitted = []
    s = Shim(initial={"rock.session": '{"v":1}'}, emit=emitted.append)
    with RemotePlaywrightSession(s, "rock.session") as path:
        open(path, "w", encoding="utf-8").write('{"v":2}')
    keys = [e["key"] for e in emitted]
    assert keys == ["rock.session"]
    assert emitted[-1]["value"] == '{"v":2}'

def test_exit_does_not_emit_when_contents_unchanged():
    emitted = []
    s = Shim(initial={"rock.session": '{"v":1}'}, emit=emitted.append)
    with RemotePlaywrightSession(s, "rock.session") as _:
        pass  # no write
    assert emitted == []
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# agent/remote_session.py
"""Context-manager wrapper that mimics core.browser_sessions.PlaywrightSession
on the agent. On enter: writes the named credential to a tempfile and yields
the path. On exit: hashes the file; if it changed, write back through the
shim (which emits credentials_updated)."""
from __future__ import annotations
import hashlib
import os
import tempfile
from typing import Optional

from agent.secrets_shim import Shim


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class RemotePlaywrightSession:
    def __init__(self, shim: Shim, key: str, *, suffix: str = ".json") -> None:
        self._shim = shim
        self._key = key
        self._suffix = suffix
        self._path: Optional[str] = None
        self._original_hash: Optional[str] = None

    def __enter__(self) -> str:
        val = self._shim.get_secret(self._key) or ""
        fd, path = tempfile.mkstemp(suffix=self._suffix, prefix="dld-sess-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(val)
        self._path = path
        self._original_hash = _sha(val.encode("utf-8"))
        return path

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self._path is not None
        try:
            with open(self._path, "rb") as f:
                new_bytes = f.read()
        except FileNotFoundError:
            new_bytes = b""
        new_hash = _sha(new_bytes)
        if new_hash != self._original_hash:
            self._shim.set_secret(self._key, new_bytes.decode("utf-8"))
        try:
            os.remove(self._path)
        except OSError:
            pass
        self._path = None
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add agent/remote_session.py agent/tests/test_remote_session.py
git commit -m "feat(agent): RemotePlaywrightSession ctx-mgr on top of secrets_shim"
```

### Task B4: `agent/run_batch.py` — orchestration skeleton

**Files:**
- Create: `agent/run_batch.py`
- Test: `agent/tests/test_run_batch.py`

- [ ] **Step 1: Failing test (stub uploaders)**

```python
# agent/tests/test_run_batch.py
import pytest
from agent import run_batch

@pytest.fixture
def stub_dispatch(monkeypatch):
    calls = []
    def _dispatch(*, platform, row, emit, paths, **_):
        calls.append({"platform": platform, "row_idx": row["row_idx"]})
        emit({"type": "event", "event": "success", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "payload": {}})
        return {"success": True}
    monkeypatch.setattr(run_batch, "_dispatch_upload", _dispatch)
    return calls

def test_run_batch_dispatches_each_row_platform_combination(stub_dispatch):
    emitted = []
    envelope = {
        "rows": [
            {"row_idx": 0, "iso_date": "2026-05-22",
             "platforms": ["YouTube Video", "Rock"],
             "entry": {"date": "2026-05-22"}, "elements": {}},
            {"row_idx": 1, "iso_date": "2026-05-23",
             "platforms": ["Simplecast"],
             "entry": {"date": "2026-05-23"}, "elements": {}},
        ],
        "config": {"max_workers": 4},
    }
    paths = {
        "2026-05-22": {"video": "/m/v22.mp4"},
        "2026-05-23": {"audio": "/m/a23.mp3"},
    }
    run_batch.run(envelope=envelope, paths=paths, emit=emitted.append)
    assert sorted((c["row_idx"], c["platform"]) for c in stub_dispatch) == [
        (0, "Rock"), (0, "YouTube Video"), (1, "Simplecast"),
    ]
    assert any(e.get("event") == "done" for e in emitted)
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement skeleton** — for first pass, run sequentially; parallel pool comes next.

```python
# agent/run_batch.py
"""Agent-side orchestration: dispatches each (row, platform) to the bundled
uploaders. Copy-and-trim of core.upload_jobs.run_batch with the db.* calls
removed (server pre-applies idempotent skip; server records upload_history
from emitted success events).
"""
from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor

_logger = logging.getLogger(__name__)


def _dispatch_upload(*, platform, row, emit, paths, **_):
    # Real per-platform dispatch lands in Task B5. The test monkey-patches
    # this so we can ship the orchestrator independently.
    raise NotImplementedError("uploader dispatch added in Task B5")


def run(*, envelope: dict, paths: dict, emit) -> None:
    """Execute the plan. `paths` is a map iso_date -> {kind: local_path}.
    `emit` is called once per event frame (already shaped as a dict)."""
    rows = envelope["rows"]
    config = envelope.get("config", {})
    max_workers = int(config.get("max_workers", 4))
    tasks = []
    for row in rows:
        for platform in row["platforms"]:
            tasks.append((platform, row))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [
            ex.submit(_run_one, platform, row, emit, paths)
            for (platform, row) in tasks
        ]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                _logger.exception("run_batch task crashed: %s", e)
    emit({"type": "event", "event": "done", "job_id": envelope.get("job_id")})


def _run_one(platform, row, emit, paths):
    try:
        _dispatch_upload(platform=platform, row=row, emit=emit, paths=paths)
    except Exception as e:
        emit({"type": "event", "event": "error", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "error": str(e)})
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add agent/run_batch.py agent/tests/test_run_batch.py
git commit -m "feat(agent): run_batch orchestrator (parallel skeleton, dispatch TBD in B5)"
```

### Task B5: `_dispatch_upload` + email-after-YouTube ordering + circuit breaker

**Files:**
- Modify: `agent/run_batch.py`
- Test: `agent/tests/test_run_batch.py`

- [ ] **Step 1: Add failing tests**

```python
def test_rock_email_waits_for_youtube_video_result(monkeypatch):
    # Email dispatcher must see the watch_url from the YouTube row.
    seen = {}
    yt_finished = []
    import threading, time
    yt_done = threading.Event()
    def _disp(*, platform, row, emit, paths, **_):
        if platform == "YouTube Video":
            time.sleep(0.05)
            emit({"type":"event","event":"success","platform":"YouTube Video",
                  "row_idx": row["row_idx"], "iso_date": row["iso_date"],
                  "payload": {"watch_url": "https://yt/x"}})
            yt_finished.append(True); yt_done.set()
            return {"success": True}
        if platform == "Rock Email":
            assert yt_done.wait(2.0), "email started before YT finished"
            seen["watch_url_at_email_start"] = row.get("yt_watch_url")
            emit({"type":"event","event":"success","platform":"Rock Email",
                  "row_idx": row["row_idx"], "iso_date": row["iso_date"], "payload": {}})
            return {"success": True}
    monkeypatch.setattr(run_batch, "_dispatch_upload", _disp)
    emitted = []
    run_batch.run(
        envelope={
            "rows": [{"row_idx": 0, "iso_date": "2026-05-22",
                       "platforms": ["YouTube Video", "Rock Email"],
                       "entry": {"date":"2026-05-22"}, "elements": {}}],
            "config": {"max_workers": 4},
        },
        paths={"2026-05-22": {"video": "/m/v.mp4"}},
        emit=emitted.append,
    )
    assert seen["watch_url_at_email_start"] == "https://yt/x"

def test_circuit_breaker_short_circuits_after_threshold(monkeypatch):
    # 3 consecutive transient failures trip the breaker; 4th call doesn't reach _disp.
    from core import circuit_breaker
    calls = {"n": 0}
    def _disp(*, platform, row, emit, paths, **_):
        calls["n"] += 1
        raise TimeoutError("network")
    monkeypatch.setattr(run_batch, "_dispatch_upload", _disp)
    emitted = []
    rows = [{"row_idx": i, "iso_date": f"2026-05-{20+i:02d}",
             "platforms": ["Rock"], "entry": {"date": f"2026-05-{20+i:02d}"},
             "elements": {}} for i in range(5)]
    run_batch.run(
        envelope={"rows": rows,
                  "config": {"max_workers": 1,
                             "circuit_breaker": {"failure_threshold": 3,
                                                 "recovery_timeout_seconds": 60}}},
        paths={r["iso_date"]: {} for r in rows},
        emit=emitted.append,
    )
    assert calls["n"] == 3
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement** — extend `_run_one` with breaker + cross-platform wait:

```python
# agent/run_batch.py — replace _run_one + add helpers
import threading
from core import circuit_breaker as _cb

_yt_done: dict[int, threading.Event] = {}      # row_idx -> Event
_yt_url: dict[int, str | None] = {}
_yt_lock = threading.Lock()

def _record_yt_result(row_idx, watch_url):
    with _yt_lock:
        _yt_url[row_idx] = watch_url
        ev = _yt_done.setdefault(row_idx, threading.Event())
        ev.set()

def _wait_yt(row_idx, timeout=1800):
    with _yt_lock:
        ev = _yt_done.setdefault(row_idx, threading.Event())
    ev.wait(timeout=timeout)
    return _yt_url.get(row_idx)


def _emit_phase(emit, platform, row, phase):
    emit({"type": "event", "event": "phase_change", "platform": platform,
          "row_idx": row["row_idx"], "iso_date": row["iso_date"], "phase": phase})


def _emit_capture(emit, captured):
    def _wrap(frame):
        captured.append(frame)
        emit(frame)
    return _wrap


def _run_one(platform, row, emit, paths):
    cb_cfg = (row.get("_config_circuit_breaker")
              or {"failure_threshold": 3, "recovery_timeout_seconds": 60})
    breaker = _cb.breaker_for(
        f"upload:{platform}",
        failure_threshold=int(cb_cfg["failure_threshold"]),
        recovery_timeout_seconds=int(cb_cfg["recovery_timeout_seconds"]),
    )
    if not breaker.allow():
        emit({"type":"event","event":"error","platform":platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "error": "circuit_breaker_open"})
        return
    # Email-after-YouTube wait
    if platform == "Rock Email" and "YouTube Video" in row["platforms"]:
        watch = _wait_yt(row["row_idx"])
        row["yt_watch_url"] = watch
    captured = []
    try:
        _dispatch_upload(platform=platform, row=row,
                         emit=_emit_capture(emit, captured), paths=paths)
        if any(f.get("event") == "success" for f in captured):
            breaker.record_success()
            if platform == "YouTube Video":
                url = next((f.get("payload", {}).get("watch_url")
                            for f in captured if f.get("event") == "success"), None)
                _record_yt_result(row["row_idx"], url)
    except (TimeoutError, OSError) as e:
        breaker.record_failure()
        emit({"type":"event","event":"error","platform":platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "error": str(e)})
        if platform == "YouTube Video":
            _record_yt_result(row["row_idx"], None)
    except Exception as e:
        # Data failure — neutral to breaker per parent design.
        emit({"type":"event","event":"error","platform":platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "error": str(e)})
        if platform == "YouTube Video":
            _record_yt_result(row["row_idx"], None)
```

Then pipe the `circuit_breaker` config into each row before dispatch — in `run()`, before building `tasks`:

```python
    cb_cfg = config.get("circuit_breaker")
    if cb_cfg:
        for row in rows:
            row["_config_circuit_breaker"] = cb_cfg
```

(Also reset `_yt_done` / `_yt_url` per call to `run()` so tests don't leak across runs.)

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add agent/run_batch.py agent/tests/test_run_batch.py
git commit -m "feat(agent): run_batch — circuit breaker + email-after-YT wait"
```

### Task B6: Real per-platform dispatch (calls into bundled uploaders)

**Files:**
- Modify: `agent/run_batch.py`

- [ ] **Step 1: Add an integration-style test using stub uploader modules**

```python
# agent/tests/test_run_batch.py — add at bottom
def test_dispatch_calls_youtube_uploader_with_resolved_video_path(monkeypatch, tmp_path):
    from uploaders import youtube_uploader
    called = {}
    def _fake_upload(*, video_path, **kw):
        called["video_path"] = video_path
        return {"success": True, "watch_url": "https://yt/y"}
    monkeypatch.setattr(youtube_uploader, "upload_video", _fake_upload)
    emitted = []
    video = tmp_path / "v.mp4"; video.write_bytes(b"x")
    run_batch.run(
        envelope={
            "rows": [{"row_idx": 0, "iso_date": "2026-05-22",
                       "platforms": ["YouTube Video"],
                       "entry": {"date": "2026-05-22", "youtube_title": "T"},
                       "elements": {"youtube_video_enabled": True,
                                     "youtube_video_thumbnail": False,
                                     "youtube_video_schedule": False}}],
            "config": {"max_workers": 1},
        },
        paths={"2026-05-22": {"video": str(video)}},
        emit=emitted.append,
    )
    assert called["video_path"] == str(video)
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement `_dispatch_upload`** — small switch on platform name:

```python
# agent/run_batch.py — replace the placeholder _dispatch_upload with:
from uploaders import youtube_uploader
from uploaders import simplecast_uploader
from uploaders.rock import orchestrator as rock_orch
from uploaders import vista_social_uploader
from uploaders.rock.email import schedule_email as rock_schedule_email


def _entry_obj(row):
    """Rebuild a lightweight entry namespace from the serialized dict.
    Path fields are injected from `paths` at the call site."""
    from types import SimpleNamespace
    return SimpleNamespace(**row["entry"])


def _dispatch_upload(*, platform, row, emit, paths, **_):
    iso = row["iso_date"]
    e = _entry_obj(row)
    p = paths.get(iso, {})
    el = row["elements"]
    emit({"type":"event","event":"start","platform":platform,
          "row_idx": row["row_idx"], "iso_date": iso})
    if platform == "YouTube Video":
        e.media_path = p.get("video")
        e.thumbnail_path = p.get("thumbnail")
        result = youtube_uploader.upload_video(
            entry=e, elements=el, kind="long",
            progress_callback=lambda **kw: emit({
                "type":"event","event":"upload_progress","platform":platform,
                "row_idx": row["row_idx"], "iso_date": iso, **kw}),
        )
    elif platform == "YouTube Shorts":
        e.media_path = p.get("short_video")
        e.thumbnail_path = p.get("short_thumbnail")
        result = youtube_uploader.upload_video(entry=e, elements=el, kind="short")
    elif platform == "Simplecast":
        e.media_path = p.get("audio")
        e.thumbnail_path = p.get("podcast_thumbnail")
        result = simplecast_uploader.upload_episode(entry=e, elements=el)
    elif platform == "Rock":
        e.media_path = p.get("video")
        e.thumbnail_path = p.get("thumbnail")
        e.spotlight_image_path = p.get("spotlight")
        e.vista_image_path = p.get("vista")
        e.reflection_image_path = p.get("reflection")
        result = rock_orch.upload_daily_experience(entry=e, elements=el)
    elif platform == "Rock Email":
        e.email_thumbnail_path = p.get("email_thumbnail")
        e.youtube_watch_url = row.get("yt_watch_url") or getattr(e, "youtube_watch_url", None)
        result = rock_schedule_email(entry=e, elements=el)
    elif platform == "Vista Social":
        e.media_path = p.get("video")
        result = vista_social_uploader.upload_post(entry=e, elements=el)
    else:
        result = {"success": False, "error": f"unknown platform {platform!r}"}
    if result.get("success"):
        emit({"type":"event","event":"success","platform":platform,
              "row_idx": row["row_idx"], "iso_date": iso,
              "payload": {k: v for k, v in result.items() if k != "success"}})
    else:
        emit({"type":"event","event":"error","platform":platform,
              "row_idx": row["row_idx"], "iso_date": iso,
              "error": result.get("error", "unknown error")})
```

(Adjust uploader signatures to match the actual functions exported by each module. The plan executor verifies each call against the real module signatures and tweaks if needed — this is checklist territory, not guesswork.)

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add agent/run_batch.py agent/tests/test_run_batch.py
git commit -m "feat(agent): run_batch dispatches to bundled uploaders w/ resolved paths"
```

### Task B7: `agent/dispatch.py` — control plane

**Files:**
- Create: `agent/dispatch.py`
- Test: `agent/tests/test_dispatch.py`

- [ ] **Step 1: Failing test**

```python
# agent/tests/test_dispatch.py
from agent import dispatch

class StubTransport:
    def __init__(self):
        self.sent = []
    def send(self, frame):
        self.sent.append(frame)

def test_handle_job_plan_installs_creds_and_runs_then_emits_done(monkeypatch, tmp_path):
    # Stub scan map: one date with a video path.
    monkeypatch.setattr(dispatch, "_resolve_paths",
                        lambda rows: {"2026-05-22": {"video": "/m/v.mp4"}})
    # Stub run_batch to verify it sees the right envelope + emit.
    seen = {}
    def _fake_run(*, envelope, paths, emit):
        seen["envelope_job"] = envelope["job_id"]
        seen["paths"] = paths
        emit({"type":"event","event":"start","platform":"YouTube Video",
              "row_idx": 0, "iso_date": "2026-05-22"})
        emit({"type":"event","event":"done"})
    monkeypatch.setattr(dispatch, "_run_batch_run", _fake_run)
    transport = StubTransport()
    dispatch.handle_job_plan(
        plan={"v":1, "type":"job_plan", "job_id":"J1", "protocol_version":1,
              "config": {"max_workers": 4},
              "rows": [{"row_idx":0,"iso_date":"2026-05-22",
                        "platforms":["YouTube Video"],
                        "entry": {"date":"2026-05-22"}, "elements": {}}],
              "credentials": {"youtube.token": "{}"}},
        transport=transport,
    )
    assert seen["envelope_job"] == "J1"
    assert seen["paths"]["2026-05-22"]["video"] == "/m/v.mp4"
    # Every emitted frame gets a job_id and is sent through the transport.
    types = [(f["type"], f.get("event")) for f in transport.sent]
    assert ("event", "start") in types
    assert ("event", "done") in types
    assert all(f.get("job_id") == "J1" for f in transport.sent)
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# agent/dispatch.py
"""Agent control plane. Receives `job_plan` frames from the transport,
installs the credentials/db shims, resolves local file paths from the
cached scan, runs the orchestrator, and pumps every emitted event back
through the transport. Phase 3 keeps the pending_results / buffer logic
deferred to PR-C."""
from __future__ import annotations
import logging
from typing import Any

from agent import scan as _scan
from agent import secrets_shim as _sshim
from agent import db_shim as _dshim
from agent import run_batch as _rb

_logger = logging.getLogger(__name__)

# Indirection so tests can monkeypatch:
_run_batch_run = _rb.run


def _resolve_paths(rows: list[dict]) -> dict[str, dict[str, str]]:
    """For each row's iso_date, return {kind: local_path} from the scan."""
    # scan.latest_results() returns the cached scan from Phase 2a, keyed by
    # date -> {kind: path}. Phase 3 verifies all media kinds are scanned;
    # see Task B8.
    cached = _scan.latest_results()
    return {row["iso_date"]: cached.get(row["iso_date"], {}) for row in rows}


def handle_job_plan(*, plan: dict, transport) -> None:
    job_id = plan["job_id"]
    def _emit(frame: dict) -> None:
        # Stamp job_id on every outgoing frame.
        if "job_id" not in frame:
            frame = {**frame, "job_id": job_id}
        try:
            transport.send(frame)
        except Exception as e:
            _logger.warning("transport.send failed: %s", e)

    # Install shims fresh for this job — credentials come from the envelope.
    _sshim.install_as_core_secrets_store(
        initial=dict(plan.get("credentials") or {}),
        emit=_emit,
    )
    _dshim.install_as_core_db(emit=_emit)

    paths = _resolve_paths(plan["rows"])
    try:
        _run_batch_run(envelope=plan, paths=paths, emit=_emit)
    except Exception as e:
        _logger.exception("run_batch crashed: %s", e)
        _emit({"type": "event", "event": "error",
               "error": f"run_batch crashed: {e}"})
        _emit({"type": "event", "event": "done"})
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add agent/dispatch.py agent/tests/test_dispatch.py
git commit -m "feat(agent): dispatch.handle_job_plan — install shims, resolve paths, run"
```

### Task B8: Verify + extend `agent/scan.py` coverage

**Files:**
- Modify: `agent/scan.py` (only if gaps found)
- Test: `agent/tests/test_scan.py` (or extend existing)

- [ ] **Step 1: Audit which media kinds each uploader actually needs.**

Read each uploader once:
- `uploaders/youtube_uploader.py` — `entry.media_path`, `entry.thumbnail_path`.
- `uploaders/simplecast_uploader.py` — `entry.media_path` (audio), `entry.thumbnail_path` (podcast thumb).
- `uploaders/rock/orchestrator.py` + children — `entry.media_path` (video), `entry.thumbnail_path`, `entry.spotlight_image_path`, `entry.vista_image_path`, `entry.reflection_image_path`.
- `uploaders/rock/email.py` — `entry.email_thumbnail_path` (separate directory per CLAUDE.md), `entry.youtube_watch_url`.
- `uploaders/vista_social_uploader.py` — `entry.media_path`.

Cross-reference what `agent/scan.py` currently produces. List any missing kind in the PR description.

- [ ] **Step 2: Failing test for the most-likely gap (`email_thumbnail`)**

```python
# agent/tests/test_scan.py — add
def test_scan_indexes_email_thumbnail_directory(tmp_roots):
    # tmp_roots has separate dirs for video/thumbnail/audio/email_thumb;
    # the scan must report email_thumbnail paths in its kind map.
    from agent import scan
    scan.set_roots(tmp_roots)
    result = scan.scan()
    assert "email_thumbnail" in result["2026-05-22"]
    assert result["2026-05-22"]["email_thumbnail"].endswith(".png")
```

- [ ] **Step 3: Run, expect FAIL (if there's a gap).**

- [ ] **Step 4: Extend `agent/scan.py`** to walk the `email_thumbnails` root and tag matches as `email_thumbnail`. (Implementation is small — adjust the existing kind-map switch.)

- [ ] **Step 5: Run, expect PASS.**

- [ ] **Step 6: Repeat Steps 2-5 for each gap found in the audit.**

- [ ] **Step 7: Commit**

```bash
git add agent/scan.py agent/tests/test_scan.py
git commit -m "feat(agent): scan now indexes every media kind uploaders need"
```

### Task B9: Wire `dispatch.handle_job_plan` into `agent/main.py`

**Files:**
- Modify: `agent/main.py`

- [ ] **Step 1: Add to the `_on_message` dispatch:**

```python
# agent/main.py — inside _on_message(conn, msg)
    elif msg.get("type") == "job_plan":
        from agent import dispatch
        # Adapt the AgentConnection to the {send: ...} shape dispatch expects.
        class _T:
            send = lambda self, frame: conn.send(frame)
        try:
            dispatch.handle_job_plan(plan=msg, transport=_T())
        except Exception as e:
            log.exception("handle_job_plan crashed: %s", e)
```

- [ ] **Step 2: Smoke run the agent against a local Flask** — pair a fresh code, POST a hand-crafted `job_plan` from a unit-test script via the relay, watch the agent logs.

Quick driver (`scripts/dispatch_smoke.py`, throwaway — don't commit):

```python
# Crafts a minimal job_plan and POSTs it via the existing relay test utility,
# verifying the agent logs "run_batch crashed" or actual upload attempts.
```

- [ ] **Step 3: Commit**

```bash
git add agent/main.py
git commit -m "feat(agent): main routes job_plan to dispatch.handle_job_plan"
```

### Task B10: Cut a new agent release and PR-B finishing commit

- [ ] **Step 1: Bump `agent/_version.py` → `0.3.0`.**

```python
__version__ = "0.3.0"
```

- [ ] **Step 2: Run the full agent test suite.**

```bash
pytest agent/tests -q
```

- [ ] **Step 3: Tag + push so the GHA release pipeline builds 0.3.0.**

```bash
git add agent/_version.py
git commit -m "chore(agent): bump version 0.3.0 (Phase 3 runner)"
git push origin feat/phase3-agent-runner
git tag agent-v0.3.0
git push origin agent-v0.3.0
```

- [ ] **Step 4: Wait for GHA → verify `/agent/releases/manifest.json` shows 0.3.0.**

- [ ] **Step 5: Live verify** — your paired agent auto-updates to 0.3.0 on next start. Trigger a dispatch with `path=agent`; watch the event stream and the platform results.

- [ ] **Step 6: Open PR-B**

```bash
gh pr create --base main --title "Phase 3 PR-B: agent-side run_batch + shims" \
  --body "Implements the agent side of Phase 3. New files:
agent/secrets_shim.py, agent/db_shim.py, agent/remote_session.py,
agent/run_batch.py, agent/dispatch.py.

Shims installed at agent startup let bundled uploaders run unchanged
against envelope-supplied credentials. run_batch is a copy-and-trim of
core.upload_jobs.run_batch minus db.* calls; the per-platform circuit
breaker and email-after-YouTube ordering are preserved.

Live-verified against autoalert.pro with agent v0.3.0."
```

---

# PR-C — Disconnect / reconciliation

### Task C1: Bounded event buffer + replay on reconnect

**Files:**
- Modify: `agent/dispatch.py` (introduce `_EventBuffer`)
- Test: `agent/tests/test_event_buffer.py`

- [ ] **Step 1: Failing test**

```python
# agent/tests/test_event_buffer.py
import time
from agent.dispatch import EventBuffer

def test_appends_when_connected_and_passes_through():
    sent = []
    buf = EventBuffer(max_size=4, send=sent.append)
    buf.set_connected(True)
    buf.emit({"type":"event","event":"start"})
    assert sent == [{"type":"event","event":"start"}]

def test_buffers_on_disconnect_and_replays_on_reconnect():
    sent = []
    buf = EventBuffer(max_size=4, send=sent.append)
    buf.set_connected(True)
    buf.set_connected(False)
    buf.emit({"type":"event","event":"a"})
    buf.emit({"type":"event","event":"b"})
    assert sent == []
    buf.set_connected(True)
    assert [f["event"] for f in sent] == ["a", "b"]

def test_buffer_drops_oldest_when_full():
    sent = []
    buf = EventBuffer(max_size=2, send=sent.append)
    buf.set_connected(False)
    for i in range(5):
        buf.emit({"type":"event","event": f"e{i}"})
    buf.set_connected(True)
    assert [f["event"] for f in sent] == ["e3", "e4"]
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# agent/dispatch.py — append
from collections import deque
import threading as _thr

class EventBuffer:
    def __init__(self, *, max_size: int, send) -> None:
        self._max = max_size
        self._send = send
        self._q: deque[dict] = deque()
        self._connected = False
        self._lock = _thr.RLock()

    def set_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected
            if connected:
                while self._q:
                    self._send(self._q.popleft())

    def emit(self, frame: dict) -> None:
        with self._lock:
            if self._connected:
                self._send(frame)
                return
            if len(self._q) >= self._max:
                self._q.popleft()
            self._q.append(frame)
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add agent/dispatch.py agent/tests/test_event_buffer.py
git commit -m "feat(agent): EventBuffer — bounded, replay on reconnect"
```

### Task C2: `pending_results` accumulation + hello-frame extension

**Files:**
- Modify: `agent/dispatch.py` (accumulate completed-row events), `agent/transport.py` (carry `pending_results` in hello, clear on ack).
- Test: `agent/tests/test_pending_results.py`

- [ ] **Step 1: Failing test**

```python
# agent/tests/test_pending_results.py
from agent.dispatch import PendingResults

def test_completed_success_event_is_recorded():
    pr = PendingResults()
    pr.observe({"type":"event","event":"success","job_id":"J1","row_idx":0,
                "iso_date":"2026-05-22","platform":"YouTube Video","payload":{}})
    assert pr.snapshot() == [
        {"job_id":"J1","row_idx":0,"iso_date":"2026-05-22",
         "platform":"YouTube Video","status":"success","payload":{}}
    ]

def test_dedup_by_job_row_platform():
    pr = PendingResults()
    pr.observe({"type":"event","event":"success","job_id":"J1","row_idx":0,
                "iso_date":"d","platform":"P","payload":{"a":1}})
    pr.observe({"type":"event","event":"success","job_id":"J1","row_idx":0,
                "iso_date":"d","platform":"P","payload":{"a":2}})
    snap = pr.snapshot()
    assert len(snap) == 1
    assert snap[0]["payload"] == {"a": 2}  # last write wins

def test_clear_on_ack_removes_acked_keys_only():
    pr = PendingResults()
    pr.observe({"type":"event","event":"success","job_id":"J1","row_idx":0,
                "iso_date":"d","platform":"P","payload":{}})
    pr.observe({"type":"event","event":"success","job_id":"J1","row_idx":1,
                "iso_date":"d","platform":"P","payload":{}})
    pr.clear_acked([("J1",0,"P")])
    assert [e["row_idx"] for e in pr.snapshot()] == [1]
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# agent/dispatch.py — append
class PendingResults:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, int, str], dict] = {}
        self._lock = _thr.RLock()

    def observe(self, frame: dict) -> None:
        if frame.get("type") != "event" or frame.get("event") != "success":
            return
        key = (frame["job_id"], frame["row_idx"], frame["platform"])
        entry = {
            "job_id": frame["job_id"],
            "row_idx": frame["row_idx"],
            "iso_date": frame["iso_date"],
            "platform": frame["platform"],
            "status": "success",
            "payload": frame.get("payload", {}),
        }
        with self._lock:
            self._by_key[key] = entry

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._by_key.values())

    def clear_acked(self, keys) -> None:
        with self._lock:
            for k in keys:
                self._by_key.pop(tuple(k), None)
```

Then in `agent/transport.py`, extend hello-frame composition:

```python
# agent/transport.py — find _hello() or equivalent
def _hello(self, pending_results=None):
    h = { ...existing fields..., }
    if pending_results:
        h["pending_results"] = pending_results
    return h
```

And in `handle_job_plan` / dispatch, wire `PendingResults` so every `success` event is observed.

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add agent/dispatch.py agent/transport.py agent/tests/test_pending_results.py
git commit -m "feat(agent): PendingResults — record success rows, hello-frame replay"
```

### Task C3: Server `pending_results` ingestion + ack

**Files:**
- Modify: `blueprints/agent.py` — handle `pending_results` in hello frame
- Modify: `core/agent_dispatch.py` — apply each entry idempotently
- Test: `tests/test_pending_results_ingest.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_pending_results_ingest.py
def test_pending_results_idempotent_application(tmp_db):
    from core import agent_dispatch, db as _db
    agent_dispatch.register_job(job_id="J9", sse_queue=None, session_id="S9")
    entries = [
        {"job_id":"J9","row_idx":0,"iso_date":"2026-05-22",
         "platform":"YouTube Video","status":"success","payload":{"watch_url":"u"}},
    ]
    # First apply records the row.
    agent_dispatch.apply_pending_results(entries)
    assert _db.has_successful_upload("S9", "2026-05-22", "YouTube Video") is True
    # Second apply is a no-op (no exception, no duplicate row).
    agent_dispatch.apply_pending_results(entries)
    rows = _db.list_upload_history("S9")
    assert sum(1 for r in rows
               if r["iso_date"] == "2026-05-22" and r["platform"] == "YouTube Video"
               and r["status"] == "success") == 1
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement** — extend `core/agent_dispatch.py`:

```python
# core/agent_dispatch.py — append
def apply_pending_results(entries: list[dict]) -> list[tuple]:
    """Apply each entry to upload_history idempotently. Returns the list
    of ack keys (tuples of job_id,row_idx,platform) the agent can clear."""
    acked: list[tuple] = []
    for e in entries:
        job = _job(e["job_id"])
        session_id = job.get("session_id") if job else None
        if session_id and not _db.has_successful_upload(
                session_id, e["iso_date"], e["platform"]):
            try:
                _db.record_upload(
                    session_id=session_id,
                    iso_date=e["iso_date"],
                    platform=e["platform"],
                    status=e["status"],
                    payload=e.get("payload") or {},
                )
            except Exception as ex:
                _logger.warning("apply_pending_results record failed: %s", ex)
                continue
        acked.append((e["job_id"], e["row_idx"], e["platform"]))
    return acked
```

Then in `blueprints/agent.py` socket handler, on hello receipt:

```python
            if msg.get("type") == "hello":
                pending = msg.get("pending_results") or []
                if pending:
                    from core import agent_dispatch
                    acked = agent_dispatch.apply_pending_results(pending)
                    ws.send(json.dumps({"v":1, "type": "pending_results_ack",
                                        "acked": acked}))
```

- [ ] **Step 4: Agent honors ack — in `agent/transport.py`** message-receive loop:

```python
                if msg.get("type") == "pending_results_ack":
                    pending_results.clear_acked(msg.get("acked") or [])
```

- [ ] **Step 5: Run, expect PASS.**

- [ ] **Step 6: Commit**

```bash
git add core/agent_dispatch.py blueprints/agent.py agent/transport.py tests/test_pending_results_ingest.py
git commit -m "feat(agent_dispatch): apply_pending_results idempotent + ack roundtrip"
```

### Task C4: Cross-path event invariant integration test

**Files:**
- Create: `tests/integration/test_agent_phase3_end_to_end.py`

- [ ] **Step 1: Failing test**

```python
# tests/integration/test_agent_phase3_end_to_end.py
"""End-to-end: dispatch a stub-uploader job through the real relay using
the agent path, and assert the browser receives the same shape of events
as the web-only path."""
import json, queue, threading, time
from core import agent_dispatch, upload_jobs

def _collect(q, until_event="done", timeout=10):
    out = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            f = q.get(timeout=0.1); out.append(f)
            if f.get("event") == until_event:
                return out
        except queue.Empty:
            continue
    return out

def test_agent_path_events_match_web_only_path(running_app_with_stub_uploaders,
                                                paired_stub_agent):
    web_q = queue.Queue()
    agent_q = queue.Queue()
    upload_jobs.run_batch(emit=web_q.put, dates=[...], summary=[...],
                          file_paths=..., session_id="S1",
                          entries_snapshot=..., skip_set=set(),
                          config={"upload": {"max_workers": 1}})
    web = _collect(web_q)
    agent_dispatch.register_job(job_id="J1", sse_queue=agent_q, session_id="S1")
    agent_dispatch.start(session_id="S1", summary=[...], entries={...},
                        elements={...}, config={"max_workers": 1})
    agent = _collect(agent_q)
    # Same set of event types in the same order modulo timing.
    assert [f["event"] for f in web if "event" in f] == \
           [f["event"] for f in agent if "event" in f]
```

(Fixtures `running_app_with_stub_uploaders` and `paired_stub_agent` build on top of the existing `tests/integration/test_agent_end_to_end.py` infrastructure — extend it rather than duplicate.)

- [ ] **Step 2: Run, expect FAIL** (until fixtures land).

- [ ] **Step 3: Extend the existing integration harness** — add stub-uploader fixtures and a fake-paired-agent helper. Use the patterns already in `tests/integration/test_agent_end_to_end.py` for the relay handshake.

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_agent_phase3_end_to_end.py
git commit -m "test(integration): event invariant between web-only and agent paths"
```

### Task C5: PR-C finishing commit + live verify

- [ ] **Step 1: Run the full suite.**

```bash
pytest -q
```

- [ ] **Step 2: Live verify reconciliation** — start a real agent-path upload, kill the agent's network briefly mid-run, restore it, confirm events catch up and `upload_history` records the final state.

- [ ] **Step 3: Open PR-C**

```bash
gh pr create --base main --title "Phase 3 PR-C: disconnect + pending_results reconciliation" \
  --body "Adds the in-memory event buffer, pending_results hello-frame extension,
server-side idempotent ingest with ack roundtrip, and the cross-path
integration test asserting the agent path emits the same event sequence
as the web-only path.

Concludes Phase 3 — the hybrid upload agent now executes real uploads."
```

---

## Self-review checklist (post-completion)

After all three PRs merge:

- [ ] Confirm `HYBRID_AGENT_ENABLED=true` still set on the VPS (unchanged).
- [ ] Run one real upload through the agent path; verify `upload_history`, refreshed sessions in `secrets_store`, and the credits file all updated.
- [ ] Run one real upload through the web-only path (chip set to "use web instead"); confirm it's unaffected.
- [ ] Update memory file `hybrid-upload-agent-status.md` to mark Phase 3 done, list any deferred follow-ups (local headed login, polished device management, `cancel_job`).

---

## Out of scope (deferred to follow-up plans)

- Local headed login on the agent when a platform session is expired.
- Polished device-management UI (rename, revoke, multi-device picker).
- `cancel_job` execution path (frame type is reserved but unused).
- Shared `core/orchestrator.py` extraction (only if drift becomes painful).
- Durable `pending_results` across agent restarts (currently in-memory only).
