# Multi-Tenant Phase δ — Concurrency + Agent Download + Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lift the global web-upload RunLock to per-user (with VPS-disk admission control), keep the agent path lock-free, add a per-org per-platform soft-lock that prevents two members from racing on the same YouTube channel, track YouTube quota usage per org, and build a polished agent download landing page with OS auto-detection, a persistent download entry in /settings/devices, an empty-state card on the dashboard for users with zero paired devices, and a one-time pairing code embedded in the download URL for one-click setup.

**Architecture:** `core/media_session.RunLock` becomes per-user instead of global. New `core/disk_admission.py` runs a pre-flight check before granting a new web upload. New `core/platform_lock.py` provides a per-org per-platform mutex backed by a `platform_locks` table (SQLite). YouTube quota tracking gains an `org_id` dimension. `blueprints/download.py` (new) serves the landing page + OS-specific binary URLs; one-time pairing codes are created on demand for the logged-in user when they hit the landing page so install→paste-code is one click.

**Tech Stack:** Python 3.11+, Flask, SQLite (mutex storage), user-agents library for OS detection, pytest.

**Spec:** `docs/superpowers/specs/2026-05-23-multi-tenant-architecture-design.md`

---

## File Structure

**Create:**
- `core/disk_admission.py` — free-space pre-flight check for web uploads; `NotEnoughDiskError` + `admit_web_run(min_free_gb)`.
- `core/platform_lock.py` — per-org per-platform mutex; `platform_locks` table; `try_acquire` / `release` / `cleanup_expired` / `PlatformLockBusy`.
- `core/os_detection.py` — `detect_os(user_agent_string) -> "windows"|"macos"|"linux"|"unknown"` using `user-agents` lib.
- `blueprints/download.py` — `/download/agent` (landing) + `/download/agent/<os>` (redirect to release binary); creates one-time pairing code for current_user.
- `templates/download_agent.html` — OS-detected landing page with Windows/macOS download buttons, embedded pairing code, copy-button, "Other platforms" link.
- `tests/test_per_user_runlock.py` — per-user RunLock semantics, agent path bypass.
- `tests/test_disk_admission.py` — free-space gate, env-var override, web vs agent gating.
- `tests/test_platform_lock.py` — acquire/release/expire/contention.
- `tests/test_yt_quota.py` — per-org quota row, bump, cap enforcement, daily reset.
- `tests/test_download_landing.py` — OS detection, pairing-code embed, rate limit, audit log.

**Modify:**
- `requirements.txt` — add `user-agents>=2.2`.
- `core/media_session.py` — `RunLock` is now keyed by `user_id`; `RunLockBusy` exception; `holder()` returns `{user_id, run_id}` per slot.
- `blueprints/media.py` — `/media/run/init` and `/media/batch/run` acquire per-user lock for web path; agent path bypasses lock entirely; pre-flight `admit_web_run` before granting; explicit `release(user_id)` in `_run_batch_worker` `finally`.
- `core/upload_jobs.py` — per-platform `try_acquire(org_id, platform, user_id)` before dispatch, `release` in finally; emits `phase_change`=`waiting_for_other_upload` with the holding user's name; queues up to 5 min then errors.
- `core/agent_dispatch.py` — same platform-lock integration (per-org soft-lock applies to both paths).
- `core/quota.py` — schema gains `org_id`; `bump_quota(org_id, units)` + `check_admission(org_id, est_units)` + `QuotaWouldExceed`.
- `core/devices.py` — `create_pairing_code_for_user(user_id, ttl_minutes=30)` that stamps `created_by_user_id` so a redeeming agent gets owned by the correct user.
- `blueprints/agent.py` — `pair_new` accepts an optional `for_user_id` argument used by `download.py`; audit-logged.
- `templates/index.html` — empty-state card when `current_user` has zero paired devices in `current_org`.
- `templates/settings.html` (existing `/settings/devices` page, file may be `templates/devices.html`) — persistent header download section.
- `app.py` — register `download_bp`; wire APScheduler job for `platform_lock.cleanup_expired` (every 5 min).
- `core/audit.py` — new event types `download.requested`, `platform_lock.contention`, `quota.exceeded`.

---

### Task 1: Add `user-agents` dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Verify current pin set**

```bash
grep -E "^user-agents" requirements.txt || echo "not present"
```

- [ ] **Step 2: Add the dependency**

```diff
--- a/requirements.txt
+++ b/requirements.txt
@@
 PyYAML>=6.0
 cryptography>=42.0
+user-agents>=2.2
```

- [ ] **Step 3: Install and import-smoke**

```bash
pip install -r requirements.txt
python -c "from user_agents import parse; print(parse('Mozilla/5.0 (Windows NT 10.0)').os.family)"
```

Expected output: `Windows`.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "deps: add user-agents>=2.2 for download-page OS detection"
```

---

### Task 2: Per-user `RunLock` (registry refactor)

**Files:**
- Modify: `core/media_session.py`
- Test: `tests/test_per_user_runlock.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/test_per_user_runlock.py
import pytest
from core.media_session import RunLock, RunLockBusy


def test_two_users_can_hold_lock_concurrently():
    lock = RunLock()
    assert lock.acquire(user_id="u1", run_id="r1") is True
    assert lock.acquire(user_id="u2", run_id="r2") is True
    assert lock.holder("u1") == {"user_id": "u1", "run_id": "r1"}
    assert lock.holder("u2") == {"user_id": "u2", "run_id": "r2"}


def test_same_user_second_acquire_raises():
    lock = RunLock()
    lock.acquire(user_id="u1", run_id="r1")
    with pytest.raises(RunLockBusy) as exc:
        lock.acquire(user_id="u1", run_id="r2")
    assert exc.value.run_id == "r1"


def test_release_only_clears_that_user():
    lock = RunLock()
    lock.acquire(user_id="u1", run_id="r1")
    lock.acquire(user_id="u2", run_id="r2")
    lock.release(user_id="u1", run_id="r1")
    assert lock.holder("u1") is None
    assert lock.holder("u2") == {"user_id": "u2", "run_id": "r2"}


def test_release_wrong_run_id_noop():
    lock = RunLock()
    lock.acquire(user_id="u1", run_id="r1")
    lock.release(user_id="u1", run_id="zzz")
    assert lock.holder("u1") == {"user_id": "u1", "run_id": "r1"}
```

- [ ] **Step 2: Run, see fail**

```bash
pytest tests/test_per_user_runlock.py -x
```

Expected: `ImportError: cannot import name 'RunLockBusy'`.

- [ ] **Step 3: Implement**

```python
# core/media_session.py — replace the existing RunLock class
import threading
from dataclasses import dataclass


class RunLockBusy(Exception):
    def __init__(self, user_id: str, run_id: str):
        super().__init__(f"user={user_id} already holds run={run_id}")
        self.user_id = user_id
        self.run_id = run_id


@dataclass
class _Slot:
    user_id: str
    run_id: str


class RunLock:
    """Per-user mutex. Each user_id can hold at most one active run_id."""

    def __init__(self) -> None:
        self._slots: dict[str, _Slot] = {}
        self._mu = threading.Lock()

    def acquire(self, *, user_id: str, run_id: str) -> bool:
        with self._mu:
            existing = self._slots.get(user_id)
            if existing is not None:
                raise RunLockBusy(user_id=user_id, run_id=existing.run_id)
            self._slots[user_id] = _Slot(user_id=user_id, run_id=run_id)
            return True

    def release(self, *, user_id: str, run_id: str) -> None:
        with self._mu:
            existing = self._slots.get(user_id)
            if existing is not None and existing.run_id == run_id:
                del self._slots[user_id]

    def holder(self, user_id: str) -> dict | None:
        with self._mu:
            s = self._slots.get(user_id)
            return None if s is None else {"user_id": s.user_id, "run_id": s.run_id}

    def all_run_ids(self) -> set[str]:
        with self._mu:
            return {s.run_id for s in self._slots.values()}
```

- [ ] **Step 4: Run, see pass**

```bash
pytest tests/test_per_user_runlock.py -x
```

- [ ] **Step 5: Audit downstream callers**

```bash
grep -rn "RunLock\|run_lock\|acquire_run_lock" --include="*.py" .
```

Note every call site for Task 3.

- [ ] **Step 6: Commit**

```bash
git add core/media_session.py tests/test_per_user_runlock.py
git commit -m "feat(concurrency): per-user RunLock keyed by user_id"
```

---

### Task 3: `blueprints/media.py` adopts per-user lock + agent bypass

**Files:**
- Modify: `blueprints/media.py`
- Test: extend `tests/test_per_user_runlock.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_per_user_runlock.py — append
def test_media_run_init_locks_per_user(client, user_a, user_b):
    with client.session_transaction() as s:
        s["user_id"] = user_a.id
    r1 = client.post("/media/run/init", json={})
    assert r1.status_code == 200
    r2 = client.post("/media/run/init", json={})
    assert r2.status_code == 409  # same user, second attempt

    with client.session_transaction() as s:
        s["user_id"] = user_b.id
    r3 = client.post("/media/run/init", json={})
    assert r3.status_code == 200  # different user, allowed


def test_agent_path_bypasses_lock(client, user_a, monkeypatch):
    from core.media_session import RunLock
    rl = RunLock()
    rl.acquire(user_id=user_a.id, run_id="held")
    monkeypatch.setattr("blueprints.media._run_lock", rl)

    with client.session_transaction() as s:
        s["user_id"] = user_a.id
    r = client.post("/media/batch/run", json={"path": "agent", "dates": []})
    assert r.status_code != 409  # agent path ignores lock
```

- [ ] **Step 2: Run, see fail**

```bash
pytest tests/test_per_user_runlock.py::test_media_run_init_locks_per_user -x
```

- [ ] **Step 3: Implement — `run_init`**

```python
# blueprints/media.py — inside run_init()
from core.media_session import RunLock, RunLockBusy
from flask_login import current_user

_run_lock = RunLock()  # module-level singleton (replaces the old global)


@bp.post("/media/run/init")
def run_init():
    user_id = current_user.id
    run_id = uuid.uuid4().hex
    try:
        _run_lock.acquire(user_id=user_id, run_id=run_id)
    except RunLockBusy as e:
        return jsonify({"error": "run_in_progress", "run_id": e.run_id}), 409
    run_dir = RunDir.allocate()
    flask.session["run_id"] = run_id
    flask.session["run_dir"] = run_dir.path
    return jsonify({"run_id": run_id, "run_dir": run_dir.path})
```

- [ ] **Step 4: Implement — `batch_run` web vs agent**

```python
# blueprints/media.py — inside batch_run()
@bp.post("/media/batch/run")
def batch_run():
    body = request.get_json(force=True)
    path = body.get("path", "web")
    user_id = current_user.id

    if path == "agent":
        # agent path is lock-free — agent runs on user's own machine
        return _start_agent_job(body)

    # web path requires the per-user lock be held from run_init
    holder = _run_lock.holder(user_id)
    if holder is None:
        return jsonify({"error": "no_active_run"}), 409
    run_id = holder["run_id"]
    # ... existing batch dispatch ...
    return jsonify({"job_id": job_id})
```

- [ ] **Step 5: Implement — `_run_batch_worker` finally**

```python
# blueprints/media.py — _run_batch_worker
def _run_batch_worker(job_id, run_id, dates, summary, file_paths, entries_snapshot, session_id, app, user_id):
    try:
        # ... existing logic ...
        pass
    finally:
        _run_lock.release(user_id=user_id, run_id=run_id)
```

- [ ] **Step 6: Run all media tests, commit**

```bash
pytest tests/test_per_user_runlock.py tests/test_media_run_init.py -x
git add blueprints/media.py tests/test_per_user_runlock.py
git commit -m "feat(concurrency): media bp uses per-user RunLock; agent path bypasses"
```

---

### Task 4: `core/disk_admission.py` — free-space pre-flight

**Files:**
- Create: `core/disk_admission.py`
- Test: `tests/test_disk_admission.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/test_disk_admission.py
import os
import shutil
import pytest
from unittest.mock import patch
from core.disk_admission import (
    admit_web_run, free_disk_bytes, NotEnoughDiskError,
)


def test_free_disk_bytes_returns_int(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_UPLOAD_TMP", str(tmp_path))
    assert free_disk_bytes() > 0


def test_admit_ok_when_above_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_UPLOAD_TMP", str(tmp_path))
    monkeypatch.setenv("DLD_DISK_ADMISSION_MIN_GB", "0")
    admit_web_run()  # no raise


def test_admit_blocks_below_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_UPLOAD_TMP", str(tmp_path))
    monkeypatch.setenv("DLD_DISK_ADMISSION_MIN_GB", "999999")
    with pytest.raises(NotEnoughDiskError) as exc:
        admit_web_run()
    assert exc.value.free_bytes >= 0
    assert exc.value.required_bytes > 0


def test_default_threshold_is_5gb(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_UPLOAD_TMP", str(tmp_path))
    monkeypatch.delenv("DLD_DISK_ADMISSION_MIN_GB", raising=False)
    with patch("core.disk_admission.shutil.disk_usage") as du:
        du.return_value = shutil._ntuple_diskusage(0, 0, 4 * 1024**3)  # 4 GiB free
        with pytest.raises(NotEnoughDiskError):
            admit_web_run()
```

- [ ] **Step 2: Run, see fail**

```bash
pytest tests/test_disk_admission.py -x
```

Expected: `ModuleNotFoundError: No module named 'core.disk_admission'`.

- [ ] **Step 3: Implement**

```python
# core/disk_admission.py
from __future__ import annotations
import os
import shutil


class NotEnoughDiskError(Exception):
    def __init__(self, free_bytes: int, required_bytes: int):
        super().__init__(
            f"insufficient disk: {free_bytes / 1024**3:.2f} GiB free, "
            f"need {required_bytes / 1024**3:.2f} GiB"
        )
        self.free_bytes = free_bytes
        self.required_bytes = required_bytes


def _upload_tmp() -> str:
    return os.environ.get("DLD_UPLOAD_TMP", "/data/uploads")


def free_disk_bytes() -> int:
    path = _upload_tmp()
    if not os.path.exists(path):
        # fall back to the parent to avoid bootstrapping issues
        path = os.path.dirname(path) or "/"
    return shutil.disk_usage(path).free


def admit_web_run(min_free_gb: float | None = None) -> None:
    if min_free_gb is None:
        min_free_gb = float(os.environ.get("DLD_DISK_ADMISSION_MIN_GB", "5"))
    required = int(min_free_gb * (1024 ** 3))
    free = free_disk_bytes()
    if free < required:
        raise NotEnoughDiskError(free_bytes=free, required_bytes=required)
```

- [ ] **Step 4: Run, see pass**

```bash
pytest tests/test_disk_admission.py -x
```

- [ ] **Step 5: Commit**

```bash
git add core/disk_admission.py tests/test_disk_admission.py
git commit -m "feat(concurrency): disk admission gate for web uploads"
```

---

### Task 5: Wire `admit_web_run` into `/media/run/init`

**Files:**
- Modify: `blueprints/media.py`
- Test: extend `tests/test_disk_admission.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_disk_admission.py — append
def test_media_run_init_returns_422_when_disk_low(client, user_a, monkeypatch):
    monkeypatch.setenv("DLD_DISK_ADMISSION_MIN_GB", "999999")
    with client.session_transaction() as s:
        s["user_id"] = user_a.id
    r = client.post("/media/run/init", json={})
    assert r.status_code == 422
    body = r.get_json()
    assert body["error"] == "vps_storage_full"
    assert "agent" in body["message"].lower()


def test_agent_path_skips_disk_check(client, user_a, monkeypatch):
    monkeypatch.setenv("DLD_DISK_ADMISSION_MIN_GB", "999999")
    with client.session_transaction() as s:
        s["user_id"] = user_a.id
    # agent path never calls /media/run/init (no temp dir needed),
    # so it cannot be blocked by disk admission
    r = client.post("/media/batch/run", json={"path": "agent", "dates": []})
    assert r.status_code != 422
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement**

```python
# blueprints/media.py — inside run_init() near the top
from core.disk_admission import admit_web_run, NotEnoughDiskError


@bp.post("/media/run/init")
def run_init():
    user_id = current_user.id
    try:
        admit_web_run()
    except NotEnoughDiskError as e:
        return jsonify({
            "error": "vps_storage_full",
            "message": "VPS storage full, please use the agent path",
            "free_bytes": e.free_bytes,
            "required_bytes": e.required_bytes,
        }), 422
    # ... existing lock acquire + RunDir.allocate() ...
```

- [ ] **Step 4: Run, see pass**

- [ ] **Step 5: Commit**

```bash
git add blueprints/media.py tests/test_disk_admission.py
git commit -m "feat(concurrency): /media/run/init pre-flights disk admission"
```

---

### Task 6: `platform_locks` table schema

**Files:**
- Create: `core/platform_lock.py` (schema half)
- Test: `tests/test_platform_lock.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/test_platform_lock.py
import sqlite3
from core.platform_lock import ensure_schema


def test_ensure_schema_idempotent(tmp_db):
    ensure_schema(tmp_db)
    ensure_schema(tmp_db)  # second call must not raise
    cur = tmp_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='platform_locks'"
    )
    assert cur.fetchone() is not None


def test_unique_constraint_org_platform(tmp_db):
    ensure_schema(tmp_db)
    tmp_db.execute(
        "INSERT INTO platform_locks(org_id, platform, locked_by_user_id, locked_at, expires_at) "
        "VALUES('o1','youtube','u1',0,9999)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.execute(
            "INSERT INTO platform_locks(org_id, platform, locked_by_user_id, locked_at, expires_at) "
            "VALUES('o1','youtube','u2',0,9999)"
        )
```

(Add a `tmp_db` fixture in `tests/conftest.py` if absent: returns an in-memory `sqlite3.connect(":memory:")`.)

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement**

```python
# core/platform_lock.py
from __future__ import annotations
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS platform_locks (
    org_id            TEXT    NOT NULL,
    platform          TEXT    NOT NULL,
    locked_by_user_id TEXT    NOT NULL,
    locked_at         INTEGER NOT NULL,
    expires_at        INTEGER NOT NULL,
    UNIQUE(org_id, platform)
);
CREATE INDEX IF NOT EXISTS idx_platform_locks_expires
    ON platform_locks(expires_at);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()
```

- [ ] **Step 4: Run, see pass**

- [ ] **Step 5: Commit**

```bash
git add core/platform_lock.py tests/test_platform_lock.py
git commit -m "feat(concurrency): platform_locks table schema"
```

---

### Task 7: `try_acquire` / `release` / `cleanup_expired`

**Files:**
- Modify: `core/platform_lock.py`
- Test: extend `tests/test_platform_lock.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_platform_lock.py — append
import pytest
from core.platform_lock import (
    try_acquire, release, cleanup_expired,
    PlatformLockBusy, ensure_schema,
)


def test_acquire_release(tmp_db):
    ensure_schema(tmp_db)
    h = try_acquire(tmp_db, org_id="o1", platform="youtube", user_id="u1", ttl_minutes=30)
    assert h is not None
    release(tmp_db, org_id="o1", platform="youtube", user_id="u1")
    # after release a new acquire works
    assert try_acquire(tmp_db, org_id="o1", platform="youtube", user_id="u2") is not None


def test_contention_raises_busy(tmp_db):
    ensure_schema(tmp_db)
    try_acquire(tmp_db, org_id="o1", platform="youtube", user_id="u1")
    with pytest.raises(PlatformLockBusy) as exc:
        try_acquire(tmp_db, org_id="o1", platform="youtube", user_id="u2")
    assert exc.value.locked_by_user_id == "u1"


def test_expired_lock_can_be_taken(tmp_db, monkeypatch):
    ensure_schema(tmp_db)
    monkeypatch.setattr("core.platform_lock._now", lambda: 100)
    try_acquire(tmp_db, org_id="o1", platform="youtube", user_id="u1", ttl_minutes=1)
    monkeypatch.setattr("core.platform_lock._now", lambda: 100 + 120)
    # past expires_at — second acquire succeeds and takes over
    assert try_acquire(tmp_db, org_id="o1", platform="youtube", user_id="u2") is not None


def test_cleanup_expired_deletes_rows(tmp_db, monkeypatch):
    ensure_schema(tmp_db)
    monkeypatch.setattr("core.platform_lock._now", lambda: 100)
    try_acquire(tmp_db, org_id="o1", platform="youtube", user_id="u1", ttl_minutes=1)
    monkeypatch.setattr("core.platform_lock._now", lambda: 100 + 9999)
    assert cleanup_expired(tmp_db) == 1
    assert tmp_db.execute("SELECT COUNT(*) FROM platform_locks").fetchone()[0] == 0


def test_release_wrong_user_noop(tmp_db):
    ensure_schema(tmp_db)
    try_acquire(tmp_db, org_id="o1", platform="youtube", user_id="u1")
    release(tmp_db, org_id="o1", platform="youtube", user_id="u_other")
    # original holder still holds
    with pytest.raises(PlatformLockBusy):
        try_acquire(tmp_db, org_id="o1", platform="youtube", user_id="u2")
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement**

```python
# core/platform_lock.py — append below ensure_schema
import sqlite3
import time


class PlatformLockBusy(Exception):
    def __init__(self, org_id: str, platform: str, locked_by_user_id: str, expires_at: int):
        super().__init__(
            f"platform={platform} for org={org_id} held by user={locked_by_user_id}"
        )
        self.org_id = org_id
        self.platform = platform
        self.locked_by_user_id = locked_by_user_id
        self.expires_at = expires_at


def _now() -> int:
    return int(time.time())


def try_acquire(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    platform: str,
    user_id: str,
    ttl_minutes: int = 30,
) -> dict:
    now = _now()
    expires_at = now + ttl_minutes * 60
    # First, atomically reap any expired row for this (org,platform).
    conn.execute(
        "DELETE FROM platform_locks "
        "WHERE org_id=? AND platform=? AND expires_at<=?",
        (org_id, platform, now),
    )
    try:
        conn.execute(
            "INSERT OR ABORT INTO platform_locks"
            "(org_id, platform, locked_by_user_id, locked_at, expires_at) "
            "VALUES(?,?,?,?,?)",
            (org_id, platform, user_id, now, expires_at),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT locked_by_user_id, expires_at FROM platform_locks "
            "WHERE org_id=? AND platform=?",
            (org_id, platform),
        ).fetchone()
        if row is None:
            # racy reap — retry once
            return try_acquire(
                conn, org_id=org_id, platform=platform,
                user_id=user_id, ttl_minutes=ttl_minutes,
            )
        raise PlatformLockBusy(
            org_id=org_id, platform=platform,
            locked_by_user_id=row[0], expires_at=row[1],
        )
    return {
        "org_id": org_id, "platform": platform,
        "locked_by_user_id": user_id, "expires_at": expires_at,
    }


def release(
    conn: sqlite3.Connection, *, org_id: str, platform: str, user_id: str
) -> None:
    conn.execute(
        "DELETE FROM platform_locks "
        "WHERE org_id=? AND platform=? AND locked_by_user_id=?",
        (org_id, platform, user_id),
    )
    conn.commit()


def cleanup_expired(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "DELETE FROM platform_locks WHERE expires_at<=?", (_now(),)
    )
    conn.commit()
    return cur.rowcount
```

- [ ] **Step 4: Run, see pass**

- [ ] **Step 5: Commit**

```bash
git add core/platform_lock.py tests/test_platform_lock.py
git commit -m "feat(concurrency): per-org platform soft-lock with TTL + cleanup"
```

---

### Task 8: Integrate `platform_lock` into upload dispatch

**Files:**
- Modify: `core/upload_jobs.py`, `core/agent_dispatch.py`
- Test: extend `tests/test_platform_lock.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_platform_lock.py — append
def test_upload_jobs_acquires_lock_per_platform(monkeypatch, fake_org, fake_user):
    from core import upload_jobs
    acquired = []
    released = []
    monkeypatch.setattr(
        "core.platform_lock.try_acquire",
        lambda conn, **kw: (acquired.append(kw), {"expires_at": 0})[1],
    )
    monkeypatch.setattr(
        "core.platform_lock.release",
        lambda conn, **kw: released.append(kw),
    )
    upload_jobs._dispatch_upload(
        entry=fake_entry(), platform="youtube",
        org_id=fake_org.id, user_id=fake_user.id, ...
    )
    assert acquired[0]["platform"] == "youtube"
    assert released[0]["platform"] == "youtube"


def test_contention_emits_waiting_phase(monkeypatch, sse_queue):
    from core import upload_jobs
    from core.platform_lock import PlatformLockBusy
    monkeypatch.setattr(
        "core.platform_lock.try_acquire",
        lambda conn, **kw: (_ for _ in ()).throw(
            PlatformLockBusy(org_id="o1", platform="youtube",
                             locked_by_user_id="u_other", expires_at=0)
        ),
    )
    # _dispatch_upload should queue + emit phase_change=waiting_for_other_upload
    # then fail after 5 min poll timeout
    ...
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement helper in `core/upload_jobs.py`**

```python
# core/upload_jobs.py
import time
from core import db
from core.platform_lock import try_acquire, release, PlatformLockBusy

_LOCK_POLL_SECONDS = 2
_LOCK_TIMEOUT_SECONDS = 300  # 5 min


def _acquire_with_wait(
    *, org_id: str, platform: str, user_id: str, emit
) -> dict:
    started = time.time()
    notified = False
    while True:
        try:
            with db.connect() as conn:
                return try_acquire(
                    conn, org_id=org_id, platform=platform,
                    user_id=user_id, ttl_minutes=30,
                )
        except PlatformLockBusy as e:
            if not notified:
                emit({
                    "type": "phase_change",
                    "phase": "waiting_for_other_upload",
                    "platform": platform,
                    "holder_user_id": e.locked_by_user_id,
                })
                notified = True
            if time.time() - started > _LOCK_TIMEOUT_SECONDS:
                raise
            time.sleep(_LOCK_POLL_SECONDS)
```

- [ ] **Step 4: Wrap `_dispatch_upload`**

```python
# core/upload_jobs.py — _dispatch_upload
def _dispatch_upload(*, entry, platform, org_id, user_id, emit, ...):
    _acquire_with_wait(org_id=org_id, platform=platform, user_id=user_id, emit=emit)
    try:
        return _do_upload(entry, platform, ...)  # existing body
    finally:
        with db.connect() as conn:
            release(conn, org_id=org_id, platform=platform, user_id=user_id)
```

- [ ] **Step 5: Mirror in `core/agent_dispatch.py`** — the agent path also goes through the per-org soft-lock so two members can't race on the same YouTube channel even if one uses the agent.

```python
# core/agent_dispatch.py — inside start()
from core.upload_jobs import _acquire_with_wait
from core.platform_lock import release
# acquire per-platform around the job_plan envelope construction, release on
# job completion or cancellation. Same emit signature so the SSE stream shows
# the "Waiting for X's upload to finish" phase.
```

- [ ] **Step 6: Run, see pass; commit**

```bash
pytest tests/test_platform_lock.py -x
git add core/upload_jobs.py core/agent_dispatch.py tests/test_platform_lock.py
git commit -m "feat(concurrency): per-org platform soft-lock around upload dispatch"
```

---

### Task 9: Decide queue-vs-fail; finalize timeout

**Files:**
- Modify: `core/upload_jobs.py` (constants only)

- [ ] **Step 1: Failing test**

```python
# tests/test_platform_lock.py — append
def test_lock_timeout_emits_friendly_error(monkeypatch, sse_queue, fake_entry):
    from core import upload_jobs
    from core.platform_lock import PlatformLockBusy

    monkeypatch.setattr(upload_jobs, "_LOCK_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(upload_jobs, "_LOCK_POLL_SECONDS", 0)
    monkeypatch.setattr(
        "core.platform_lock.try_acquire",
        lambda conn, **kw: (_ for _ in ()).throw(
            PlatformLockBusy(org_id="o1", platform="youtube",
                             locked_by_user_id="u_other", expires_at=0)
        ),
    )
    events = []
    upload_jobs._dispatch_upload(
        entry=fake_entry, platform="youtube",
        org_id="o1", user_id="u_me",
        emit=events.append,
    )
    err = [e for e in events if e.get("type") == "error"]
    assert err
    assert "still uploading" in err[0]["message"].lower()
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement (catch the bubbled `PlatformLockBusy` and translate)**

```python
# core/upload_jobs.py — _dispatch_upload, around the acquire
def _dispatch_upload(*, entry, platform, org_id, user_id, emit, ...):
    try:
        _acquire_with_wait(org_id=org_id, platform=platform, user_id=user_id, emit=emit)
    except PlatformLockBusy as e:
        holder_name = _resolve_user_display(e.locked_by_user_id) or "another teammate"
        emit({
            "type": "error",
            "phase": "platform_lock_timeout",
            "platform": platform,
            "message": f"{holder_name} is still uploading to {platform}. "
                       f"Please retry in a few minutes.",
        })
        return {"success": False, "skipped": True, "reason": "platform_lock_timeout"}
    # ... rest of dispatch ...
```

- [ ] **Step 4: Run, see pass; commit**

```bash
git add core/upload_jobs.py tests/test_platform_lock.py
git commit -m "feat(concurrency): 5-min wait timeout w/ friendly platform-lock error"
```

---

### Task 10: Surface "waiting" phase to the browser SSE

**Files:**
- Modify: `blueprints/media.py` (or `blueprints/upload.py` if it exists)
- Test: `tests/test_platform_lock_sse.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/test_platform_lock_sse.py
def test_sse_stream_includes_waiting_phase(client, user_a, monkeypatch):
    # Force a contention scenario, subscribe to SSE, parse out phase_change
    monkeypatch.setattr(
        "core.upload_jobs._acquire_with_wait",
        lambda **kw: kw["emit"]({
            "type": "phase_change",
            "phase": "waiting_for_other_upload",
            "holder_user_id": "u_other",
        }),
    )
    monkeypatch.setattr(
        "core.users.display_name", lambda uid: "Alex" if uid == "u_other" else "?"
    )
    job_id = _start_test_job(client, user_a)
    events = list(_read_sse(client, f"/upload/stream?job_id={job_id}"))
    waiting = [e for e in events if e.get("phase") == "waiting_for_other_upload"]
    assert waiting
    assert waiting[0]["holder_display_name"] == "Alex"
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement — enrich emit with display name**

```python
# core/upload_jobs.py — _acquire_with_wait
from core.users import display_name


def _acquire_with_wait(*, org_id, platform, user_id, emit) -> dict:
    started = time.time()
    notified = False
    while True:
        try:
            with db.connect() as conn:
                return try_acquire(conn, org_id=org_id, platform=platform,
                                   user_id=user_id, ttl_minutes=30)
        except PlatformLockBusy as e:
            if not notified:
                emit({
                    "type": "phase_change",
                    "phase": "waiting_for_other_upload",
                    "platform": platform,
                    "holder_user_id": e.locked_by_user_id,
                    "holder_display_name": display_name(e.locked_by_user_id) or "another teammate",
                })
                notified = True
            if time.time() - started > _LOCK_TIMEOUT_SECONDS:
                raise
            time.sleep(_LOCK_POLL_SECONDS)
```

- [ ] **Step 4: Update `static/js/dld_pipeline.js`**

```js
// static/js/dld_pipeline.js — phase handler
if (ev.phase === "waiting_for_other_upload") {
    const name = ev.holder_display_name || "another teammate";
    setRowStatus(ev.row_idx, `Waiting for ${name}'s upload to finish…`);
}
```

- [ ] **Step 5: Run, see pass; commit**

```bash
git add core/upload_jobs.py static/js/dld_pipeline.js tests/test_platform_lock_sse.py
git commit -m "feat(concurrency): SSE surfaces 'waiting for X' on platform-lock contention"
```

---

### Task 11: YouTube quota becomes per-org

**Files:**
- Modify: `core/quota.py`
- Test: `tests/test_yt_quota.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/test_yt_quota.py
import pytest
from core.quota import (
    bump_quota, check_admission, get_quota_used, QuotaWouldExceed, ensure_schema,
)


def test_per_org_isolation(tmp_db):
    ensure_schema(tmp_db)
    bump_quota(tmp_db, org_id="o1", units=100)
    bump_quota(tmp_db, org_id="o2", units=200)
    assert get_quota_used(tmp_db, org_id="o1") == 100
    assert get_quota_used(tmp_db, org_id="o2") == 200


def test_check_admission_passes_when_under_cap(tmp_db):
    ensure_schema(tmp_db)
    bump_quota(tmp_db, org_id="o1", units=100)
    check_admission(tmp_db, org_id="o1", est_units=50, daily_cap=10000)


def test_check_admission_raises_when_over(tmp_db):
    ensure_schema(tmp_db)
    bump_quota(tmp_db, org_id="o1", units=9950)
    with pytest.raises(QuotaWouldExceed) as exc:
        check_admission(tmp_db, org_id="o1", est_units=100, daily_cap=10000)
    assert exc.value.used == 9950
    assert exc.value.est_units == 100
    assert exc.value.cap == 10000


def test_daily_reset_via_date_key(tmp_db, monkeypatch):
    ensure_schema(tmp_db)
    monkeypatch.setattr("core.quota._today_key", lambda: "2026-05-23")
    bump_quota(tmp_db, org_id="o1", units=5000)
    monkeypatch.setattr("core.quota._today_key", lambda: "2026-05-24")
    assert get_quota_used(tmp_db, org_id="o1") == 0
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement**

```python
# core/quota.py — replace existing module
from __future__ import annotations
import sqlite3
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


class QuotaWouldExceed(Exception):
    def __init__(self, *, org_id: str, used: int, est_units: int, cap: int):
        super().__init__(
            f"org={org_id} youtube quota would exceed: {used}+{est_units}>{cap}"
        )
        self.org_id = org_id
        self.used = used
        self.est_units = est_units
        self.cap = cap


_SCHEMA = """
CREATE TABLE IF NOT EXISTS yt_quota_usage (
    org_id     TEXT NOT NULL,
    date_key   TEXT NOT NULL,
    units_used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (org_id, date_key)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _today_key() -> str:
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def get_quota_used(conn: sqlite3.Connection, *, org_id: str) -> int:
    row = conn.execute(
        "SELECT units_used FROM yt_quota_usage WHERE org_id=? AND date_key=?",
        (org_id, _today_key()),
    ).fetchone()
    return int(row[0]) if row else 0


def bump_quota(conn: sqlite3.Connection, *, org_id: str, units: int) -> int:
    key = _today_key()
    conn.execute(
        "INSERT INTO yt_quota_usage(org_id, date_key, units_used) VALUES(?,?,?) "
        "ON CONFLICT(org_id, date_key) DO UPDATE SET units_used=units_used+excluded.units_used",
        (org_id, key, units),
    )
    conn.commit()
    return get_quota_used(conn, org_id=org_id)


def check_admission(
    conn: sqlite3.Connection, *, org_id: str, est_units: int, daily_cap: int = 10000
) -> None:
    used = get_quota_used(conn, org_id=org_id)
    if used + est_units > daily_cap:
        raise QuotaWouldExceed(
            org_id=org_id, used=used, est_units=est_units, cap=daily_cap
        )
```

- [ ] **Step 4: Run, see pass; commit**

```bash
git add core/quota.py tests/test_yt_quota.py
git commit -m "feat(quota): per-org YouTube quota with admission check"
```

---

### Task 12: Wire `check_admission` into both dispatch paths

**Files:**
- Modify: `core/upload_jobs.py`, `core/agent_dispatch.py`
- Test: extend `tests/test_yt_quota.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_yt_quota.py — append
def test_upload_jobs_skips_youtube_when_quota_exceeded(monkeypatch, sse_queue, fake_entry):
    from core import upload_jobs
    from core.quota import QuotaWouldExceed
    monkeypatch.setattr(
        "core.quota.check_admission",
        lambda conn, **kw: (_ for _ in ()).throw(
            QuotaWouldExceed(org_id="o1", used=9950, est_units=100, cap=10000)
        ),
    )
    events = []
    upload_jobs._dispatch_upload(
        entry=fake_entry, platform="youtube",
        org_id="o1", user_id="u1", emit=events.append,
    )
    err = [e for e in events if e.get("type") == "error"]
    assert err
    assert "quota" in err[0]["message"].lower()


def test_non_youtube_platforms_skip_quota_check(monkeypatch, sse_queue, fake_entry):
    from core import upload_jobs
    called = []
    monkeypatch.setattr(
        "core.quota.check_admission",
        lambda conn, **kw: called.append(kw),
    )
    upload_jobs._dispatch_upload(
        entry=fake_entry, platform="simplecast",
        org_id="o1", user_id="u1", emit=lambda e: None,
    )
    assert called == []
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement**

```python
# core/upload_jobs.py
from core.quota import check_admission, bump_quota, QuotaWouldExceed

# rough estimate: video upload 1600 units (1500 upload + ~100 thumb/playlist)
_YT_EST_UNITS = {"youtube": 1600, "youtube_shorts": 1600}


def _dispatch_upload(*, entry, platform, org_id, user_id, emit, ...):
    est = _YT_EST_UNITS.get(platform)
    if est is not None:
        try:
            with db.connect() as conn:
                check_admission(conn, org_id=org_id, est_units=est)
        except QuotaWouldExceed as e:
            emit({
                "type": "error",
                "phase": "quota_exceeded",
                "platform": platform,
                "message": f"YouTube daily quota would exceed: "
                           f"{e.used}+{e.est_units}>{e.cap}. "
                           f"Try again tomorrow or use a different YouTube channel.",
            })
            return {"success": False, "skipped": True, "reason": "quota_exceeded"}
    # ... existing acquire_with_wait + dispatch ...
    # On success, bump the actual units consumed:
    if est is not None and result.get("success"):
        with db.connect() as conn:
            bump_quota(conn, org_id=org_id, units=est)
    return result
```

- [ ] **Step 4: Mirror in `core/agent_dispatch.py`** — the agent path consumes the same YouTube quota since the org's `client_secrets.json` is shared.

```python
# core/agent_dispatch.py — inside start() per-row plan build
# Before adding a youtube row to the envelope:
try:
    with db.connect() as conn:
        check_admission(conn, org_id=org_id, est_units=_YT_EST_UNITS[platform])
except QuotaWouldExceed as e:
    emit({...quota_exceeded...})
    continue  # skip this row from the envelope
```

- [ ] **Step 5: Run, see pass; commit**

```bash
git add core/upload_jobs.py core/agent_dispatch.py tests/test_yt_quota.py
git commit -m "feat(quota): admission-check YouTube quota before dispatch on both paths"
```

---

### Task 13: `blueprints/download.py` — landing page + binary redirects

**Files:**
- Create: `blueprints/download.py`
- Test: `tests/test_download_landing.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/test_download_landing.py
def test_landing_requires_auth(client):
    r = client.get("/download/agent")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_landing_renders_windows_for_windows_ua(client, user_a):
    with client.session_transaction() as s:
        s["user_id"] = user_a.id
    r = client.get(
        "/download/agent",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    assert r.status_code == 200
    assert b"Download for Windows" in r.data
    assert b"Download for macOS" in r.data  # both buttons always shown
    # Windows button is the primary CTA
    assert b'data-primary-os="windows"' in r.data


def test_landing_embeds_pairing_code(client, user_a, monkeypatch):
    monkeypatch.setattr(
        "core.devices.create_pairing_code_for_user",
        lambda user_id, ttl_minutes=30: "CODE-1234",
    )
    with client.session_transaction() as s:
        s["user_id"] = user_a.id
    r = client.get("/download/agent",
                   headers={"User-Agent": "Mozilla/5.0 (Macintosh)"})
    assert b"CODE-1234" in r.data


def test_windows_binary_redirects_to_release(client, user_a):
    with client.session_transaction() as s:
        s["user_id"] = user_a.id
    r = client.get("/download/agent/windows", follow_redirects=False)
    assert r.status_code == 302
    assert "github.com" in r.headers["Location"]
    assert ".exe" in r.headers["Location"]


def test_macos_binary_redirects_to_release(client, user_a):
    with client.session_transaction() as s:
        s["user_id"] = user_a.id
    r = client.get("/download/agent/macos", follow_redirects=False)
    assert r.status_code == 302
    assert "github.com" in r.headers["Location"]
    assert (".dmg" in r.headers["Location"]) or (".zip" in r.headers["Location"])
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement**

```python
# blueprints/download.py
from __future__ import annotations
import os
from flask import Blueprint, render_template, redirect, request, abort
from flask_login import current_user, login_required

from core.os_detection import detect_os
from core.devices import create_pairing_code_for_user
from core.audit import log_event

bp = Blueprint("download", __name__)


def _release_url(os_name: str) -> str:
    base = os.environ.get(
        "DLD_AGENT_RELEASE_BASE",
        "https://github.com/G0Osey99/DailyLifeDistributor/releases/latest/download",
    )
    if os_name == "windows":
        return f"{base}/DLD-Agent-Setup.exe"
    if os_name == "macos":
        return f"{base}/DLD-Agent.dmg"
    abort(404)


@bp.get("/download/agent")
@login_required
def agent_landing():
    detected = detect_os(request.headers.get("User-Agent", ""))
    if detected not in ("windows", "macos"):
        detected = "windows"  # safe default
    code = create_pairing_code_for_user(
        user_id=current_user.id, ttl_minutes=30
    )
    log_event("download.requested", user_id=current_user.id, os=detected)
    return render_template(
        "download_agent.html",
        primary_os=detected,
        pairing_code=code,
        windows_url="/download/agent/windows",
        macos_url="/download/agent/macos",
    )


@bp.get("/download/agent/windows")
@login_required
def agent_windows():
    return redirect(_release_url("windows"), code=302)


@bp.get("/download/agent/macos")
@login_required
def agent_macos():
    return redirect(_release_url("macos"), code=302)
```

- [ ] **Step 4: Register in app.py**

```python
# app.py
from blueprints.download import bp as download_bp
app.register_blueprint(download_bp)
```

- [ ] **Step 5: Run, see pass; commit**

```bash
git add blueprints/download.py tests/test_download_landing.py app.py
git commit -m "feat(download): agent download landing + OS-specific binary redirects"
```

---

### Task 14: `templates/download_agent.html`

**Files:**
- Create: `templates/download_agent.html`

- [ ] **Step 1: Implement template**

```html
{# templates/download_agent.html #}
{% extends "base.html" %}
{% block title %}Download the DLD Agent{% endblock %}
{% block content %}
<div class="container" data-primary-os="{{ primary_os }}">
  <h1>Run uploads from your computer</h1>
  <p class="lede">
    Install the DLD Agent so the heavy media stays on your laptop and
    only metadata travels over the network. We detected
    <strong>{{ primary_os|capitalize }}</strong>.
  </p>

  <div class="download-row">
    <a class="btn btn-primary {% if primary_os == 'windows' %}btn-recommended{% endif %}"
       href="{{ windows_url }}">
      Download for Windows
    </a>
    <a class="btn btn-primary {% if primary_os == 'macos' %}btn-recommended{% endif %}"
     href="{{ macos_url }}">
      Download for macOS
    </a>
  </div>

  <section class="pairing">
    <h2>Your one-time pairing code</h2>
    <p>This code is valid for 30 minutes. Paste it into the agent on first launch.</p>
    <div class="pairing-code">
      <code id="pairing-code">{{ pairing_code }}</code>
      <button class="btn btn-ghost" data-copy="#pairing-code">Copy</button>
    </div>
  </section>

  <section class="docs">
    <h2>Other platforms / docs</h2>
    <p>
      Linux build:
      <a href="https://github.com/G0Osey99/DailyLifeDistributor/releases/latest">
        See releases
      </a>
    </p>
    <p>
      <a href="/docs/agent-install">Install troubleshooting</a>
    </p>
  </section>
</div>

<script>
  document.querySelectorAll("[data-copy]").forEach(btn => {
    btn.addEventListener("click", () => {
      const target = document.querySelector(btn.dataset.copy);
      navigator.clipboard.writeText(target.textContent.trim());
      btn.textContent = "Copied!";
      setTimeout(() => (btn.textContent = "Copy"), 1500);
    });
  });
</script>
{% endblock %}
```

- [ ] **Step 2: Verify the existing tests now pass**

```bash
pytest tests/test_download_landing.py -x
```

- [ ] **Step 3: Commit**

```bash
git add templates/download_agent.html
git commit -m "feat(download): download landing template w/ OS-aware CTA + copy button"
```

---

### Task 15: `create_pairing_code_for_user` in `core/devices.py`

**Files:**
- Modify: `core/devices.py`
- Test: `tests/test_devices_pairing_for_user.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/test_devices_pairing_for_user.py
from core.devices import (
    create_pairing_code_for_user, redeem_pairing_code, get_device_owner,
)


def test_pairing_code_is_owned_by_creator(tmp_db, user_a):
    code = create_pairing_code_for_user(user_id=user_a.id, ttl_minutes=30)
    assert len(code) >= 8
    device_id, token = redeem_pairing_code(code, device_name="My Laptop")
    assert get_device_owner(device_id) == user_a.id


def test_pairing_code_expires(tmp_db, user_a, monkeypatch):
    import time
    monkeypatch.setattr("core.devices._now",
                        lambda: __import__("datetime").datetime.fromtimestamp(100))
    code = create_pairing_code_for_user(user_id=user_a.id, ttl_minutes=1)
    monkeypatch.setattr("core.devices._now",
                        lambda: __import__("datetime").datetime.fromtimestamp(100 + 9999))
    assert redeem_pairing_code(code, device_name="X") is None
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement**

```python
# core/devices.py — append
import secrets
from datetime import timedelta
from core import db


def create_pairing_code_for_user(*, user_id: str, ttl_minutes: int = 30) -> str:
    """Create a pairing code that, when redeemed, ties the device to user_id."""
    raw = secrets.token_urlsafe(12).upper()
    code_hash = _hash(raw)
    expires_at = _now() + timedelta(minutes=ttl_minutes)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO pairing_codes(code_hash, created_by_user_id, "
            "created_at, expires_at, redeemed_at) "
            "VALUES(?,?,?,?,NULL)",
            (code_hash, user_id, _now().isoformat(), expires_at.isoformat()),
        )
        conn.commit()
    return raw


def get_device_owner(device_id: str) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM devices WHERE device_id=?",
            (device_id,),
        ).fetchone()
        return row[0] if row else None
```

(Schema migration: add `owner_user_id` column to `devices` if missing — assume PR-α already added it; otherwise:

```python
# core/db.py migration
ALTER TABLE devices ADD COLUMN owner_user_id TEXT;
ALTER TABLE pairing_codes ADD COLUMN created_by_user_id TEXT;
```
)

- [ ] **Step 4: Update `redeem_pairing_code` to copy `created_by_user_id` → `devices.owner_user_id`**

```python
# core/devices.py — redeem_pairing_code, inside the SELECT row handler
row = conn.execute(
    "SELECT id, created_by_user_id FROM pairing_codes "
    "WHERE code_hash=? AND redeemed_at IS NULL AND expires_at>?",
    (code_hash, _now().isoformat()),
).fetchone()
if not row:
    return None
pc_id, owner_user_id = row
# ... existing device insert, but with owner_user_id:
conn.execute(
    "INSERT INTO devices(device_id, name, token_hash, hwid_hash, hostname, "
    "owner_user_id, created_at) VALUES(?,?,?,?,?,?,?)",
    (device_id, device_name, _hash(token), hwid_hash, hostname,
     owner_user_id, _now().isoformat()),
)
```

- [ ] **Step 5: Run, see pass; commit**

```bash
git add core/devices.py tests/test_devices_pairing_for_user.py core/db.py
git commit -m "feat(devices): create_pairing_code_for_user binds device to creator"
```

---

### Task 16: Dashboard empty-state card

**Files:**
- Modify: `templates/index.html`
- Test: `tests/test_dashboard_empty_state.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/test_dashboard_empty_state.py
def test_empty_state_shown_when_zero_devices(client, user_no_devices):
    with client.session_transaction() as s:
        s["user_id"] = user_no_devices.id
    r = client.get("/")
    assert r.status_code == 200
    assert b"Download the agent to upload from your computer" in r.data
    assert b"/download/agent" in r.data


def test_empty_state_hidden_when_one_device(client, user_with_device):
    with client.session_transaction() as s:
        s["user_id"] = user_with_device.id
    r = client.get("/")
    assert b"Download the agent to upload from your computer" not in r.data
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement — context provider**

```python
# app.py — add a context processor
from core.devices import list_devices_for_user


@app.context_processor
def inject_user_device_count():
    if not getattr(current_user, "is_authenticated", False):
        return {"user_device_count": 0}
    return {
        "user_device_count": len(
            list_devices_for_user(current_user.id, current_org.id)
        )
    }
```

- [ ] **Step 4: Implement — template snippet**

```html
{# templates/index.html — near the top of the dashboard #}
{% if user_device_count == 0 %}
<div class="card card--cta card--empty-state">
  <div class="card__body">
    <h3>No paired devices yet</h3>
    <p>
      Download the agent to upload from your computer. The agent keeps
      heavy media files on your machine and only streams metadata to the
      server.
    </p>
    <a class="btn btn-primary" href="{{ url_for('download.agent_landing') }}">
      Download the agent
    </a>
  </div>
</div>
{% endif %}
```

- [ ] **Step 5: Run, see pass; commit**

```bash
git add templates/index.html app.py tests/test_dashboard_empty_state.py
git commit -m "feat(ui): dashboard empty-state card for users without paired devices"
```

---

### Task 17: Persistent download header in `/settings/devices`

**Files:**
- Modify: `templates/settings.html` (or `templates/devices.html`)
- Test: `tests/test_devices_page_download_section.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/test_devices_page_download_section.py
def test_devices_page_shows_persistent_download(client, user_with_device):
    with client.session_transaction() as s:
        s["user_id"] = user_with_device.id
    r = client.get("/settings/devices")
    assert r.status_code == 200
    assert b"Download the agent" in r.data
    assert b'href="/download/agent"' in r.data
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement — header section**

```html
{# templates/settings.html — top of the devices page #}
<section class="settings-devices__download-cta">
  <div class="row">
    <div class="col">
      <h3>Need to install on another machine?</h3>
      <p>
        Download the agent. Each install gets its own pairing code from the
        download page.
      </p>
    </div>
    <div class="col col--right">
      <a class="btn btn-primary" href="/download/agent">Download the agent</a>
      <a class="link link--muted"
         href="https://github.com/G0Osey99/DailyLifeDistributor/releases/latest">
        Other platforms / docs
      </a>
    </div>
  </div>
</section>
```

- [ ] **Step 4: Run, see pass; commit**

```bash
git add templates/settings.html tests/test_devices_page_download_section.py
git commit -m "feat(ui): persistent agent download CTA on /settings/devices"
```

---

### Task 18: `core/os_detection.py`

**Files:**
- Create: `core/os_detection.py`
- Test: `tests/test_os_detection.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/test_os_detection.py
import pytest
from core.os_detection import detect_os


@pytest.mark.parametrize("ua,expected", [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "windows"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3)", "macos"),
    ("Mozilla/5.0 (X11; Linux x86_64)", "linux"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_detect_os(ua, expected):
    assert detect_os(ua) == expected
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement**

```python
# core/os_detection.py
from __future__ import annotations
from user_agents import parse


_DESKTOP = {"Windows": "windows", "Mac OS X": "macos", "Linux": "linux"}


def detect_os(user_agent_string: str | None) -> str:
    if not user_agent_string:
        return "unknown"
    ua = parse(user_agent_string)
    if ua.is_mobile or ua.is_tablet:
        return "unknown"
    return _DESKTOP.get(ua.os.family, "unknown")
```

- [ ] **Step 4: Run, see pass; commit**

```bash
git add core/os_detection.py tests/test_os_detection.py
git commit -m "feat(download): OS detection helper using user-agents"
```

---

### Task 19: Audit log entries

**Files:**
- Modify: `core/audit.py`, `blueprints/download.py`, `core/upload_jobs.py`, `core/quota.py`
- Test: `tests/test_audit_events.py` (extend existing or new)

- [ ] **Step 1: Failing test**

```python
# tests/test_audit_events.py — append
def test_download_requested_audited(client, user_a):
    with client.session_transaction() as s:
        s["user_id"] = user_a.id
    client.get("/download/agent",
               headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0)"})
    events = audit_events_for(user_a.id)
    assert any(e["event"] == "download.requested" and e["meta"]["os"] == "windows"
               for e in events)


def test_platform_lock_contention_audited(monkeypatch, fake_org, fake_user):
    from core import upload_jobs
    from core.platform_lock import PlatformLockBusy
    monkeypatch.setattr(
        "core.platform_lock.try_acquire",
        lambda conn, **kw: (_ for _ in ()).throw(
            PlatformLockBusy(org_id="o1", platform="youtube",
                             locked_by_user_id="u_other", expires_at=0)
        ),
    )
    monkeypatch.setattr(upload_jobs, "_LOCK_TIMEOUT_SECONDS", 0)
    upload_jobs._dispatch_upload(
        entry=fake_entry(), platform="youtube",
        org_id="o1", user_id="u1", emit=lambda e: None,
    )
    events = audit_events_for_org("o1")
    assert any(e["event"] == "platform_lock.contention" for e in events)


def test_quota_exceeded_audited(monkeypatch, fake_org, fake_user):
    from core import upload_jobs
    from core.quota import QuotaWouldExceed
    monkeypatch.setattr(
        "core.quota.check_admission",
        lambda conn, **kw: (_ for _ in ()).throw(
            QuotaWouldExceed(org_id="o1", used=9950, est_units=100, cap=10000)
        ),
    )
    upload_jobs._dispatch_upload(
        entry=fake_entry(), platform="youtube",
        org_id="o1", user_id="u1", emit=lambda e: None,
    )
    assert any(e["event"] == "quota.exceeded" for e in audit_events_for_org("o1"))
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement — already in download.py from Task 13; add to upload_jobs and quota**

```python
# core/upload_jobs.py — in the PlatformLockBusy translation block
from core.audit import log_event

log_event(
    "platform_lock.contention",
    org_id=org_id, user_id=user_id,
    meta={"platform": platform, "holder_user_id": e.locked_by_user_id},
)

# ... and in the QuotaWouldExceed block
log_event(
    "quota.exceeded",
    org_id=org_id, user_id=user_id,
    meta={"platform": platform, "used": e.used, "est_units": e.est_units, "cap": e.cap},
)
```

- [ ] **Step 4: Run, see pass; commit**

```bash
git add core/upload_jobs.py blueprints/download.py tests/test_audit_events.py
git commit -m "feat(audit): log download.requested, platform_lock.contention, quota.exceeded"
```

---

### Task 20: Rate-limit `/download/agent`

**Files:**
- Modify: `blueprints/download.py`
- Test: extend `tests/test_download_landing.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_download_landing.py — append
def test_landing_rate_limited_at_60_per_hour(client, user_a):
    with client.session_transaction() as s:
        s["user_id"] = user_a.id
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0)"}
    for i in range(60):
        assert client.get("/download/agent", headers=headers).status_code == 200
    r = client.get("/download/agent", headers=headers)
    assert r.status_code == 429


def test_binary_redirects_not_rate_limited(client, user_a):
    with client.session_transaction() as s:
        s["user_id"] = user_a.id
    # 100 binary fetches should all succeed
    for _ in range(100):
        r = client.get("/download/agent/windows", follow_redirects=False)
        assert r.status_code == 302
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement — reuse existing limiter from `blueprints/agent.py`**

```python
# blueprints/download.py
from flask_limiter.util import get_remote_address

# Module-level; attach the actual Limiter in app.py via attach_limits().
_LANDING_LIMIT = "60 per hour"


def attach_limits(app, limiter) -> None:
    limiter.limit(_LANDING_LIMIT, key_func=get_remote_address)(agent_landing)
    # binary redirects intentionally unlimited
```

```python
# app.py — wire it
from blueprints.download import attach_limits as attach_download_limits

attach_download_limits(app, limiter)
```

- [ ] **Step 4: Run, see pass; commit**

```bash
git add blueprints/download.py app.py tests/test_download_landing.py
git commit -m "feat(download): rate-limit landing at 60/hr/IP; binary redirects unlimited"
```

---

### Task 21: Schedule `platform_lock.cleanup_expired` + final test sweep

**Files:**
- Modify: `app.py`
- Test: `tests/test_platform_lock_cleanup_job.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/test_platform_lock_cleanup_job.py
def test_apscheduler_runs_cleanup_expired(app, monkeypatch):
    called = []
    monkeypatch.setattr(
        "core.platform_lock.cleanup_expired",
        lambda conn: called.append(True) or 0,
    )
    # Inspect scheduler jobs
    jobs = [j for j in app.scheduler.get_jobs() if j.id == "platform_lock_cleanup"]
    assert len(jobs) == 1
    # Trigger immediately
    jobs[0].func()
    assert called
```

- [ ] **Step 2: Run, see fail**

- [ ] **Step 3: Implement**

```python
# app.py — wire APScheduler job (scheduler already exists from PR-α)
from core.platform_lock import cleanup_expired
from core import db


def _run_platform_lock_cleanup():
    with db.connect() as conn:
        cleanup_expired(conn)


scheduler.add_job(
    _run_platform_lock_cleanup,
    "interval", minutes=5,
    id="platform_lock_cleanup", replace_existing=True,
)
```

- [ ] **Step 4: Full test sweep**

```bash
pytest tests/ -x
```

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_platform_lock_cleanup_job.py
git commit -m "feat(concurrency): APScheduler job reaps expired platform locks every 5m"
```

---

### Task 22: Self-review + PR description draft

**Files:**
- (no code changes)

- [ ] **Step 1: Self-review the full diff**

```bash
git log --oneline main..HEAD
git diff main...HEAD --stat
```

Walk every file with the **superpowers:requesting-code-review** skill checklist:
- per-user RunLock: any leftover global `_run_lock` references?
- agent path lock-free: confirmed via `test_agent_path_bypasses_lock`?
- platform_lock TTL: any path that acquires without a finally release?
- quota: agent path bumps quota the same as web path?
- download landing: pairing code is short-lived (30 min) and one-use?
- empty-state: counts only devices in `current_org`, not across orgs?

- [ ] **Step 2: Run `superpowers:verification-before-completion` checklist**

```bash
# Manual smoke test
flask run &
xdg-open http://localhost:8080/download/agent
# Verify OS detection, copy button, two install buttons, pairing code visible
```

- [ ] **Step 3: Draft commit message + PR body**

```markdown
# PR-δ: Concurrency rework + agent download landing

## Concurrency
- `RunLock` is now per-user. Two members of the same org can run independent
  web uploads simultaneously without colliding.
- Pre-flight `admit_web_run` rejects with `422 vps_storage_full` when free
  disk on `DLD_UPLOAD_TMP` drops below `DLD_DISK_ADMISSION_MIN_GB` (default 5 GiB).
- `platform_locks` SQLite table backs a per-org per-platform soft-lock with a
  30-minute TTL. Contention shows up in the SSE stream as
  `phase_change=waiting_for_other_upload`, with the holder's display name.
  Wait timeout = 5 min, then a friendly error.
- YouTube quota now keyed by `(org_id, date_key)`. Admission check runs
  before dispatch on both web and agent paths.

## Agent download
- New `/download/agent` landing page with OS auto-detection
  (`user-agents` library), Windows + macOS download buttons, and a 30-minute
  one-time pairing code so install→paste-code is one click.
- Dashboard empty-state card for users with zero paired devices in current org.
- Persistent download CTA on `/settings/devices`.
- Rate-limited to 60/hr/IP on the landing route; binary redirects unlimited
  (GitHub serves the actual file).

## Audit
- New event types: `download.requested`, `platform_lock.contention`, `quota.exceeded`.

## Tests
- 5 new test files, ~40 new test cases. Full `pytest` clean.
```

- [ ] **Step 4: Do not push or open the PR** — parent agent batches all four PRs.

---
