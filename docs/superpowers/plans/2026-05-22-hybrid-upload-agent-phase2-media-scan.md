# Hybrid Upload Agent — Phase 2a: Media Scan & Available-Dates Reporting

> **Status:** Shipped on 2026-05-23 (consolidated in the `codebase-completion-pass` branch — see git history for the actual per-commit work). The `- [ ]` checkboxes below are TDD step artifacts kept as-is for reference; all steps were executed.

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let the agent scan its configured local media folders and report the available dates (and which files per date per category) back to the browser over the existing Phase 1 relay, in response to a `scan_request` control message.

**Architecture:** Purely agent-side. The agent reuses `core/file_scanner.parse_names` (which groups filenames by ISO date with no filesystem access). A new `agent/scan.py` lists files in each configured root and aggregates a report; `agent/main.py`'s message handler answers `scan_request` with a `scan_result`. The server relay already forwards arbitrary JSON envelopes, so **no server changes and no feature flag** are needed. The dashboard UI that renders the dates is deferred to the coexistence-UI phase (Phase 5); this slice is proven via an integration test that plays the browser's role.

**Tech Stack:** Python, `core/file_scanner` (existing), the Phase 1 relay + `simple-websocket`, `pytest`.

**Scope note:** Phase 2 in the design spec also lists the auto-update framework; that requires a packaging/build pipeline that does not yet exist and is only meaningfully testable on real machines, so it is split into its own later sub-project. This plan is the media-scan slice only.

**Message protocol (extends the Phase 1 envelope `{"v":1,"type":...,"payload":...}`):**
- Browser → agent: `{"v":1,"type":"scan_request","payload":{}}`
- Agent → browser: `{"v":1,"type":"scan_result","payload":{"by_date":{iso:{cat:[files]}}, "dates":[iso,...], "errors":{cat:msg}}}`

---

## File Structure
- `agent/scan.py` — *create*: `scan_roots(roots: dict[str,str]) -> dict`. Pure aggregation over the filesystem; reuses `core.file_scanner.parse_names`. One responsibility: turn configured roots into an available-dates report.
- `agent/main.py` — *modify*: extend `_on_message` to dispatch `scan_request` → run `scan_roots(config.get_media_roots())` → `conn.send` a `scan_result`.
- `tests/test_agent_scan.py` — *create*: unit tests for `scan_roots` against temp dirs.
- `tests/test_agent_main_dispatch.py` — *create*: unit test that `_on_message` answers a `scan_request` with a `scan_result`.
- `tests/integration/test_agent_scan_e2e.py` — *create*: full round-trip — browser sends `scan_request`, agent (configured with temp roots) replies `scan_result` with the expected dates.

> **Note for the implementer:** `agent/scan.py` imports `from core.file_scanner import parse_names`. This is the ONE place the agent reaches into `core/` — it's a pure, dependency-free function (no Flask, no DB), so the agent package stays cleanly runnable on a client. Do not import anything else from `core/`.

---

### Task 1: `agent/scan.py` — scan roots into an available-dates report

**Files:**
- Create: `agent/scan.py`
- Test: `tests/test_agent_scan.py`

- [ ] **Step 1: Write the failing tests** in `tests/test_agent_scan.py`:

```python
import os
from agent import scan


def _touch(path):
    with open(path, "w") as f:
        f.write("x")


def test_scan_groups_files_by_date_and_category(tmp_path):
    vids = tmp_path / "vids"; shorts = tmp_path / "shorts"
    vids.mkdir(); shorts.mkdir()
    # YYMMDD-prefixed names (parse_names interprets YY as 20YY).
    _touch(vids / "260115_sermon.mp4")
    _touch(vids / "260116_sermon.mp4")
    _touch(shorts / "260115_short.mp4")
    _touch(vids / "notes.txt")          # non-media: ignored
    _touch(vids / "no_date_here.mp4")   # undated: ignored

    report = scan.scan_roots({"video": str(vids), "shorts": str(shorts)})

    assert report["dates"] == ["2026-01-15", "2026-01-16"]
    assert report["by_date"]["2026-01-15"] == {
        "video": ["260115_sermon.mp4"], "shorts": ["260115_short.mp4"]}
    assert report["by_date"]["2026-01-16"] == {"video": ["260116_sermon.mp4"]}
    assert report["errors"] == {}


def test_scan_reports_missing_dir_as_error(tmp_path):
    report = scan.scan_roots({"video": str(tmp_path / "does_not_exist")})
    assert report["dates"] == []
    assert report["by_date"] == {}
    assert "video" in report["errors"]


def test_scan_empty_roots():
    report = scan.scan_roots({})
    assert report == {"by_date": {}, "dates": [], "errors": {}}
```

- [ ] **Step 2: Run, confirm FAIL** (`ModuleNotFoundError: agent.scan`): `python -m pytest tests/test_agent_scan.py -q`

- [ ] **Step 3: Implement `agent/scan.py`:**

```python
"""Scan configured local media folders into an available-dates report.

Reuses core.file_scanner.parse_names (pure, no-DB, no-Flask) to map filenames
to ISO dates. The agent owns filesystem access; the browser only renders the
report this produces. Top-level files per root are scanned (one folder per
category, matching how the dashboard pickers are organized)."""
from __future__ import annotations

import os

from core.file_scanner import parse_names


def scan_roots(roots: dict) -> dict:
    """roots maps category -> directory path. Returns:

        {"by_date": {iso: {category: [filename, ...]}},
         "dates":   [iso, ...] sorted ascending,
         "errors":  {category: message}}   # unreadable/missing dirs
    """
    by_date: dict[str, dict[str, list]] = {}
    errors: dict[str, str] = {}

    for category, path in roots.items():
        try:
            names = [n for n in os.listdir(path)
                     if os.path.isfile(os.path.join(path, n))]
        except OSError as e:
            errors[category] = str(e)
            continue
        for iso, files in parse_names(names).items():
            by_date.setdefault(iso, {})[category] = files

    return {
        "by_date": by_date,
        "dates": sorted(by_date.keys()),
        "errors": errors,
    }
```

- [ ] **Step 4: Run, confirm PASS** (3 passed): `python -m pytest tests/test_agent_scan.py -q`

- [ ] **Step 5: Commit:**
```bash
git add agent/scan.py tests/test_agent_scan.py
git commit -m "feat(agent): scan configured media roots into available-dates report"
```

---

### Task 2: Dispatch `scan_request` in `agent/main.py`

**Files:**
- Modify: `agent/main.py` (extend `_on_message`)
- Test: `tests/test_agent_main_dispatch.py`

The current `_on_message(conn, msg)` only handles `ping`:
```python
def _on_message(conn: AgentConnection, msg: dict) -> None:
    if msg.get("type") == "ping":
        conn.send({"v": 1, "type": "pong", "payload": msg.get("payload", {})})
```

- [ ] **Step 1: Write the failing test** in `tests/test_agent_main_dispatch.py`:

```python
from agent import main as agent_main


class _FakeConn:
    def __init__(self): self.sent = []
    def send(self, message): self.sent.append(message)


def test_ping_still_answered_with_pong():
    conn = _FakeConn()
    agent_main._on_message(conn, {"v": 1, "type": "ping", "payload": {"n": 1}})
    assert conn.sent == [{"v": 1, "type": "pong", "payload": {"n": 1}}]


def test_scan_request_answered_with_scan_result(monkeypatch):
    conn = _FakeConn()
    fake_report = {"by_date": {"2026-01-15": {"video": ["a.mp4"]}},
                   "dates": ["2026-01-15"], "errors": {}}
    monkeypatch.setattr(agent_main.config, "get_media_roots", lambda: {"video": "/x"})
    monkeypatch.setattr(agent_main.scan, "scan_roots", lambda roots: fake_report)

    agent_main._on_message(conn, {"v": 1, "type": "scan_request", "payload": {}})

    assert conn.sent == [{"v": 1, "type": "scan_result", "payload": fake_report}]


def test_unknown_type_ignored():
    conn = _FakeConn()
    agent_main._on_message(conn, {"v": 1, "type": "whatever", "payload": {}})
    assert conn.sent == []
```

- [ ] **Step 2: Run, confirm FAIL** (scan_request not handled / `agent_main.scan` missing): `python -m pytest tests/test_agent_main_dispatch.py -q`

- [ ] **Step 3: Edit `agent/main.py`.** Add `from agent import config, pair, scan` (add `scan` to the existing `from agent import config, pair` import), and replace `_on_message`:

```python
def _on_message(conn: AgentConnection, msg: dict) -> None:
    mtype = msg.get("type")
    if mtype == "ping":
        conn.send({"v": 1, "type": "pong", "payload": msg.get("payload", {})})
    elif mtype == "scan_request":
        report = scan.scan_roots(config.get_media_roots())
        conn.send({"v": 1, "type": "scan_result", "payload": report})
```

- [ ] **Step 4: Run, confirm PASS** (3 passed): `python -m pytest tests/test_agent_main_dispatch.py -q`

- [ ] **Step 5: Commit:**
```bash
git add agent/main.py tests/test_agent_main_dispatch.py
git commit -m "feat(agent): answer scan_request with scan_result"
```

---

### Task 3: End-to-end scan over the relay

**Files:**
- Test: `tests/integration/test_agent_scan_e2e.py`

- [ ] **Step 1: Write the integration test** `tests/integration/test_agent_scan_e2e.py`:

```python
"""End-to-end: browser asks the agent (over the relay) to scan local media."""
import json
import threading
import time

import pytest

simple_websocket = pytest.importorskip("simple_websocket")


def _touch(p):
    with open(p, "w") as f:
        f.write("x")


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
    yield port, devices, tmp_path
    srv.shutdown()


def test_scan_request_roundtrip(live, monkeypatch):
    port, devices, tmp_path = live
    server_url = f"http://127.0.0.1:{port}"

    vids = tmp_path / "vids"; vids.mkdir()
    _touch(vids / "260115_sermon.mp4")
    _touch(vids / "260116_sermon.mp4")

    code = devices.create_pairing_code()
    _, token = devices.redeem_pairing_code(code, "Mac")
    from agent import config
    monkeypatch.setattr(config, "get_token", lambda: token)
    monkeypatch.setattr(config, "get_server_url", lambda: server_url)
    monkeypatch.setattr(config, "get_media_roots", lambda: {"video": str(vids)})

    from agent import main as agent_main
    threading.Thread(target=agent_main.run, args=(server_url,), daemon=True).start()
    time.sleep(0.8)

    import requests
    s = requests.Session(); s.post(f"{server_url}/login", data={"password": "pw"})
    cookie = "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())
    browser = simple_websocket.Client(f"ws://127.0.0.1:{port}/agent/ws",
                                      headers={"Cookie": cookie})
    for _ in range(5):
        first = json.loads(browser.receive(timeout=5))
        if first.get("type") == "presence" and first["payload"]["online"] is True:
            break
    else:
        pytest.fail("agent never reported presence=online within 5 messages")

    browser.send(json.dumps({"v": 1, "type": "scan_request", "payload": {}}))
    result = json.loads(browser.receive(timeout=5))
    assert result["type"] == "scan_result"
    assert result["payload"]["dates"] == ["2026-01-15", "2026-01-16"]
    assert result["payload"]["by_date"]["2026-01-15"]["video"] == ["260115_sermon.mp4"]
    browser.close()
```

- [ ] **Step 2: Run, confirm PASS** (1 passed): `python -m pytest tests/integration/test_agent_scan_e2e.py -q`. If timing-flaky, you may adjust the sleep/loop counts (as in the Phase 1 e2e test) but do NOT weaken the core assertion (the browser receives a `scan_result` whose `dates` are exactly `["2026-01-15", "2026-01-16"]`).

- [ ] **Step 3: Run the full suite:** `python -m pytest -q` → all pass, the 3 pre-existing live-cred integration tests skip.

- [ ] **Step 4: Commit:**
```bash
git add tests/integration/test_agent_scan_e2e.py
git commit -m "test(agent): end-to-end scan_request -> scan_result over the relay"
```

---

## Acceptance
- The agent scans its configured media roots and reports `{by_date, dates, errors}`; non-media and undated files are ignored; missing dirs surface as per-category errors.
- A browser connected to the relay can send `scan_request` and receives a `scan_result` with the correct dates — proven end to end.
- `ping`/`pong` still works; unknown message types are ignored.
- No server changes, no feature-flag changes; the agent package still only imports the pure `parse_names` from `core/`.

## Deferred (later sub-projects)
- Auto-update framework + signed release feed + packaging pipeline (its own plan).
- Job dispatch / plan builder + running real uploaders against the scanned files (Phase 3).
- Dashboard UI that renders the scanned dates and routes a run to the agent (Phase 5 coexistence UI).
