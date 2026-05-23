# Browser-Streaming Media Pipeline — Implementation Plan

> **Status:** Shipped on 2026-05-23 (consolidated in the `codebase-completion-pass` branch — see git history for the actual per-commit work). The `- [ ]` checkboxes below are TDD step artifacts kept as-is for reference; all steps were executed.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the server-side directory-scan model with a browser-driven, stream-through pipeline: users pick media folders + a spreadsheet in the browser (no install), files upload to the VPS in chunked batches of 4 dates, get consumed by all platforms that need them (deduped by physical file), and are deleted — keeping the ~80 GB VPS within ~20 GB transient disk.

**Architecture:** Browser holds `webkitdirectory` File refs and orchestrates the run. It sends filenames for date matching, uploads a persisted spreadsheet for column mapping, then per batch of 4 dates: chunk-uploads the distinct files → triggers a server batch-run (existing parallel `ThreadPoolExecutor`, deduped by file, idempotent skip of already-succeeded rows) → server deletes the batch's temp files → next batch. Whisper is removed; title suggestions use a mapped transcript column → Ollama. Per-browser-session settings; one run at a time.

**Tech Stack:** Python 3.12 / Flask, existing uploaders (YouTube API, Playwright SimpleCast/Vista/Rock), SQLite (`upload_history`), vanilla JS (File API `slice()` chunking, `fetch`, SSE), Docker Compose on the VPS.

**Design spec:** `docs/superpowers/specs/2026-05-22-browser-streaming-media-pipeline-design.md` — read it first; it carries the full decision log and rationale.

---

## Conventions & Ground Rules

- **Read before editing.** Several existing modules are integration points; their exact signatures must be read at execution time (flagged per task). Don't trust remembered line numbers.
- **Tests:** `python -m pytest` from the repo root. Use the existing autouse fixtures in `tests/conftest.py` (`_isolate_state_db`, `temp_db`, `_isolate_config`, `_master_key`).
- **Commit after each task** (and after green tests). Branch: `feat/headless-remote-login` (PR pending — keep building here).
- **Flask session** = per-browser cookie session (`from flask import session as flask_session`); do NOT confuse with the `core.session_state.session` workflow singleton.
- **Deploy to verify (later tasks):** `cd ~/DailyLifeDistributor && git pull && cd deploy && docker compose up -d --build` on the VPS (`wsl ssh dropshippa`).

---

## Phase A — Backend foundations (unit-testable, no UI)

## Task 1: Per-session settings store (`core/media_session.py`)

Holds per-browser-session settings (column mapping, selected dates) in the Flask session, and process-level run state (run lock, temp-dir lifecycle, orphan sweep, free-space check). Pure logic + thin Flask-session accessors so it's unit-testable.

**Files:**
- Create: `core/media_session.py`
- Create: `tests/test_media_session.py`

- [ ] **Step 1: Write failing tests**

```python
"""Per-session settings + run-state for the browser-streaming pipeline."""
import os
import pytest
from core import media_session as ms


def test_temp_root_under_data(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))
    run = ms.RunDir.allocate()
    assert os.path.isdir(run.path)
    assert run.path.startswith(str(tmp_path / "uploads"))
    run.cleanup()
    assert not os.path.exists(run.path)


def test_file_path_is_namespaced(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))
    run = ms.RunDir.allocate()
    fid = run.new_file_id()                 # server-issued uuid
    p = run.file_path(fid)
    # Must stay inside the run dir regardless of anything.
    assert os.path.realpath(p).startswith(os.path.realpath(run.path))
    run.cleanup()


def test_run_lock_single_holder(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))
    lock = ms.RunLock()
    assert lock.acquire("run-a") is True
    assert lock.acquire("run-b") is False   # already held
    lock.release("run-a")
    assert lock.acquire("run-b") is True
    lock.release("run-b")


def test_orphan_sweep_removes_stale(monkeypatch, tmp_path):
    root = tmp_path / "uploads"
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(root))
    run = ms.RunDir.allocate()
    # Simulate an orphan: a run dir with no active lock.
    ms.sweep_orphans(active_run_ids=set())
    assert not os.path.exists(run.path)


def test_free_space_check(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path))
    # 0 required always fits; an absurd requirement never does.
    assert ms.has_free_space(0) is True
    assert ms.has_free_space(10**18) is False
```

- [ ] **Step 2: Run, verify failure** — `python -m pytest tests/test_media_session.py -v` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement `core/media_session.py`**

```python
"""Per-session settings + per-run temp/lock management for the streaming pipeline.

Two scopes:
  * Per browser session (Flask session cookie): column mapping + selected dates.
  * Per process: a single run lock (one upload at a time) and per-run temp dirs
    that hold transiently-uploaded media, plus an orphan sweep.
"""
from __future__ import annotations

import os
import shutil
import threading
import uuid
from dataclasses import dataclass

# Temp media lives on the data volume (DLD mounts dld-data at /data on the VPS).
# Falls back to a repo-local dir for local/dev runs.
_TEMP_ROOT = os.environ.get("DLD_UPLOAD_TMP") or (
    "/data/uploads" if os.path.isdir("/data") else
    os.path.join(os.path.dirname(os.path.dirname(__file__)), ".uploads")
)


@dataclass
class RunDir:
    run_id: str
    path: str

    @classmethod
    def allocate(cls) -> "RunDir":
        run_id = uuid.uuid4().hex
        path = os.path.join(_TEMP_ROOT, run_id)
        os.makedirs(path, exist_ok=True)
        if os.name != "nt":
            os.chmod(path, 0o700)
        return cls(run_id=run_id, path=path)

    def new_file_id(self) -> str:
        return uuid.uuid4().hex

    def file_path(self, file_id: str) -> str:
        # file_id is server-issued (uuid hex); reject anything else so a crafted
        # value can't escape the run dir.
        if not file_id or any(c not in "0123456789abcdef" for c in file_id):
            raise ValueError("bad file_id")
        return os.path.join(self.path, file_id)

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


class RunLock:
    """One upload run at a time. Holder identified by run_id so the same run
    can re-enter / release; a different run is refused while one is active."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder: str | None = None

    def acquire(self, run_id: str) -> bool:
        with self._lock:
            if self._holder is not None and self._holder != run_id:
                return False
            self._holder = run_id
            return True

    def release(self, run_id: str) -> None:
        with self._lock:
            if self._holder == run_id:
                self._holder = None

    def holder(self) -> str | None:
        with self._lock:
            return self._holder


def sweep_orphans(active_run_ids: set[str]) -> int:
    """Remove any temp run dir not in active_run_ids. Returns count removed."""
    removed = 0
    if not os.path.isdir(_TEMP_ROOT):
        return 0
    for name in os.listdir(_TEMP_ROOT):
        if name in active_run_ids:
            continue
        full = os.path.join(_TEMP_ROOT, name)
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)
            removed += 1
    return removed


def has_free_space(required_bytes: int) -> bool:
    """True if the temp volume can hold required_bytes with a safety margin."""
    os.makedirs(_TEMP_ROOT, exist_ok=True)
    usage = shutil.disk_usage(_TEMP_ROOT)
    margin = 2 * 1024 * 1024 * 1024  # keep 2 GB headroom
    return usage.free >= required_bytes + margin
```

- [ ] **Step 4: Run tests** → `python -m pytest tests/test_media_session.py -v` → all pass.

- [ ] **Step 5: Commit**

```bash
git add core/media_session.py tests/test_media_session.py
git commit -m "feat(media): per-session settings + per-run temp/lock/sweep helpers"
```

---

## Task 2: Filename-only date scan (`core/file_scanner.py`)

Expose a pure function that parses dates from a list of **filenames** (no filesystem), reusing the existing date-extraction logic, so the browser can send names and the server returns the date→file map.

**Files:**
- Read first: `core/file_scanner.py` (find the existing per-filename date-parsing helper — the multi-format YYMMDD/DDMMYY/etc. extractor and the 6-digit ambiguity handling). Reuse it; do not duplicate the regex.
- Modify: `core/file_scanner.py` (add `parse_names`)
- Test: `tests/test_file_scanner_names.py`

- [ ] **Step 1: Write the failing test** — exercises the same date formats already covered by `tests/test_file_scanner_dates.py` (read that file to mirror expectations) but via the new name-list API:

```python
from core.file_scanner import parse_names

def test_parse_names_groups_by_iso_date():
    names = ["DailyLife_250521.mp4", "DailyLife_250522.mp4", "notes.txt"]
    out = parse_names(names)            # -> {iso_date: [filename, ...]}
    assert "2025-05-21" in out
    assert "DailyLife_250521.mp4" in out["2025-05-21"]
    assert "notes.txt" not in str(out)  # non-media / undated ignored

def test_parse_names_six_digit_ambiguity_offered():
    # Mirror the existing ambiguity behavior from test_file_scanner_dates.py.
    out = parse_names(["clip_010203.mp4"])
    # Assert whatever the existing scanner does for ambiguous 6-digit dates
    # (e.g. both interpretations surfaced). Match the existing test's shape.
    assert out  # refine to the real contract after reading file_scanner.py
```

- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement `parse_names(names: list[str]) -> dict[str, list[str]]`** in `file_scanner.py`, delegating to the existing date-parsing helper for each name and applying the existing media-extension allowlist (so `.DS_Store`/`.txt` are ignored). Keep the 6-digit ambiguity behavior identical to the directory scanner.
- [ ] **Step 4: Run tests** → pass. Also run `python -m pytest tests/test_file_scanner_dates.py -v` to confirm no regression.
- [ ] **Step 5: Commit** — `feat(scan): parse_names() for filename-only date matching`.

---

## Task 3: Transcript column in the Excel mapping (`core/excel_parser.py`)

Add `transcript_column` to the mapping and surface its text per date; allow parsing a spreadsheet at an arbitrary path (the uploaded/cached file).

**Files:**
- Read first: `core/excel_parser.py` (how mapping keys are read, how a row maps to a date/metadata; the cache).
- Modify: `core/excel_parser.py`
- Test: `tests/test_excel_transcript_column.py`

- [ ] **Step 1: Failing test** — build a tiny `.xlsx` in `tmp_path` (use `openpyxl`, already a dep) with a date column + a transcript column, map them, assert the parser returns the transcript text for the date.

```python
import openpyxl
from core.excel_parser import parse_spreadsheet   # add/adjust to real entrypoint

def test_transcript_column_extracted(tmp_path):
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Date", "Transcript"])
    ws.append(["2025-05-21", "Today we talk about gratitude."])
    p = tmp_path / "sheet.xlsx"; wb.save(p)
    mapping = {"sheet_name": ws.title, "date_column": "Date", "transcript_column": "Transcript"}
    rows = parse_spreadsheet(str(p), mapping)
    assert rows["2025-05-21"]["transcript"] == "Today we talk about gratitude."
```

- [ ] **Step 2–4:** Implement `transcript_column` handling (read the real parser API first; adapt the test entrypoint to it). Add `transcript` to the per-date metadata dict / `ReviewEntry`. Run tests.
- [ ] **Step 5: Commit** — `feat(excel): map a transcript column for LLM titles`.

---

## Task 4: Remove Whisper; titles from transcript text → Ollama

**Files:**
- Read first: `blueprints/review.py` (the title-suggestion job path; it currently calls transcription + `core.llm_title_gen`), `core/llm_title_gen.py` (`generate_title_suggestions(transcript, ...)` already takes text), `core/transcriber.py`, `requirements.txt`, `templates/settings.html` (Whisper section), `config.yaml` (`whisper:`), `deploy/Dockerfile` (ffmpeg).
- Delete: `core/transcriber.py`
- Modify: `blueprints/review.py`, `requirements.txt`, `templates/settings.html`, `config.yaml`, `deploy/Dockerfile` (drop `ffmpeg` only if nothing else uses it — grep first)
- Test: `tests/test_review_titles_from_transcript.py`

- [ ] **Step 1: Failing test** — the review title-suggestion path, given an entry whose `transcript` is set, calls `generate_title_suggestions` with that text and returns suggestions (monkeypatch `llm_title_gen.generate_title_suggestions` to echo its input). Assert no transcription is attempted.
- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement** — in `review.py`, replace the transcribe-then-suggest flow with: take `entry.transcript` (from Task 3) → `generate_title_suggestions(text)`. Remove imports of `transcriber`. Delete `core/transcriber.py`. Remove `faster-whisper` from `requirements.txt`. Remove the Whisper `<section>` from `settings.html` and `whisper:` from `config.yaml`. Grep `ffmpeg` usage repo-wide; if Whisper was the only consumer, drop it from `deploy/Dockerfile`.
- [ ] **Step 4: Run** `python -m pytest -q` (full suite — ensure nothing imported `transcriber`).
- [ ] **Step 5: Commit** — `refactor(llm): drop Whisper; titles from mapped transcript column`.

---

## Task 5: Spreadsheet upload + column-mapping endpoints (`blueprints/media.py`)

**Files:**
- Read first: `app.py` (blueprint registration block), `blueprints/settings.py` (how excel sheet/column listing currently works — there's a `/settings/excel-sheets` route to mirror).
- Create: `blueprints/media.py`
- Modify: `app.py` (register blueprint)
- Test: `tests/test_media_spreadsheet.py`

Endpoints (all auth-gated automatically by the global gate):
- `POST /media/spreadsheet` (multipart `file`) → save to the session's run-scoped cache path, return `{sheets: [...]}`.
- `GET /media/spreadsheet/columns?sheet=...` → `{columns: [...]}`.
- `POST /media/mapping` (JSON) → store mapping in the Flask session; `GET /media/mapping` → return it.

- [ ] **Step 1: Failing test** (uses `temp_db`, logs in via the shared password like `tests/test_auth_routes.py`): post a small xlsx, assert 200 + sheet names; set a mapping, GET it back from the session.
- [ ] **Step 2–4:** Implement. Cap size (e.g. ≤ 5 MB). Store the xlsx under a per-session cache file (filename = session id) so it persists for the session; parse sheets/columns with `openpyxl`. Mapping persists in `flask.session["excel_mapping"]`.
- [ ] **Step 5: Commit** — `feat(media): spreadsheet upload + session column mapping`.

---

## Task 6: Chunked upload endpoint + reassembly (`blueprints/media.py`)

**Files:**
- Modify: `blueprints/media.py`
- Test: `tests/test_media_chunk_upload.py`

Endpoints:
- `POST /media/run/init` → acquires the run lock, allocates a `RunDir`, returns `{run_id}`. 409 if a run is already active.
- `POST /media/upload/chunk` (multipart) fields: `run_id`, `file_id` (server-issued via an init or allocated on first chunk — see below), `chunk_index`, `total_chunks`, `data`. Appends to `RunDir.file_path(file_id)`; on the last chunk returns `{complete: true, bytes: N}`.
- File-id allocation: `POST /media/file/new?run_id=...` → `{file_id}` (server uuid). Browser asks once per physical file, then sends its chunks.

- [ ] **Step 1: Failing tests:** init a run; allocate a file-id; upload 3 small chunks out of a known payload; assert the reassembled temp file equals the payload and the last chunk returns `complete`. Assert a bad `file_id` is rejected (400) and a chunk over the size cap is rejected. Assert `/media/upload/chunk` without an active run is rejected.
- [ ] **Step 2–4:** Implement. Enforce per-chunk cap (`_MAX_CHUNK = 95 * 1024 * 1024`), append in `chunk_index` order (reject out-of-order or duplicate gracefully), and a per-run declared-total cap checked against `media_session.has_free_space`. Hold a module-level `RunLock` + dict of active `RunDir` by `run_id`.
- [ ] **Step 5: Commit** — `feat(media): chunked upload + reassembly with caps`.

---

## Task 7: Batch run — dedup by file, idempotent skip, temp paths (`core/upload_jobs.py`)

Refactor the upload runner to operate on **one batch of dates** against **temp file paths**, deduping by physical file and skipping `(date, platform)` already succeeded.

**Files:**
- Read first: `core/upload_jobs.py` (`_run_upload_job`, `_upload_one`, the `emit(...)` events, the email-after-YouTube wait, `_db.record_upload`), `core/db.py` (add a "was this row already successful?" query), `core/session_state.py` (`ReviewEntry` fields the uploaders read: `podcast_path`, video paths, `email_thumbnail_path`, etc.).
- Modify: `core/upload_jobs.py`, `core/db.py`
- Test: `tests/test_batch_upload_jobs.py`

- [ ] **Step 1: Failing tests** (fake uploaders via monkeypatch on the `*_upload_*` symbols in `upload_jobs`):
  - **Dedup:** two platforms in the batch pointing at the same temp file → the file is "consumed" once (assert each uploader still runs, but a shared-file counter increments once). 
  - **Idempotent skip:** seed `upload_history` with a `success` row for `(date, "YouTube Video")`; run the batch; assert that row is **skipped** (skip event emitted, uploader not called) while the others run.
  - **Email ordering:** within a batch, the `Rock Email` task waits for the `YouTube Video` task's `watch?v=` URL (reuse/keep the existing mechanism).
- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.**
  - Add `core/db.has_successful_upload(session_id, iso_date, platform) -> bool`.
  - New entrypoint `run_batch(dates, summary, file_paths, session_id, emit, skip_set, ...)` that: builds the task list for the batch; **before submitting each task**, skips it if `has_successful_upload(...)`; points each `ReviewEntry`/summary item at its temp path from `file_paths` (keyed by category+date); groups by physical file for dedup; runs the existing `ThreadPoolExecutor` (max_workers from config); preserves the email-waits-for-YouTube wait.
  - Keep all existing per-row SSE events (`start/progress/success/error/skip/needs_manual/done`).
- [ ] **Step 4: Run tests** → pass. Run full suite.
- [ ] **Step 5: Commit** — `feat(upload): batch runner with file dedup + idempotent skip`.

---

## Task 8: Batch-run route + delete + lifecycle (`blueprints/media.py`)

**Files:**
- Modify: `blueprints/media.py`, `app.py` (orphan sweep on startup), `blueprints/remote_login.py` (or wherever the idle reaper lives — reuse the daemon to also sweep orphans)
- Test: `tests/test_media_batch_run.py`

- `POST /media/batch/run` (JSON): `run_id`, `dates`, `platforms`, and `files` (the file-id→(category,date) map). Validates **all file-ids for the batch are `complete`** (the reassembly handshake; 409 otherwise), kicks `upload_jobs.run_batch` against the temp paths with SSE (reuse `/upload/stream`), and on completion **deletes the batch's temp files**.
- `POST /media/run/finish` → release the run lock + `RunDir.cleanup()` (also called in a `finally`).
- Startup + idle reaper call `media_session.sweep_orphans(active_run_ids)`.

- [ ] **Step 1: Failing tests:** batch-run rejected (409) if a file-id isn't complete; happy path runs the (fake) batch and deletes the temp files afterward; a second `/media/run/init` while a run is active returns 409 (busy guard); `sweep_orphans` removes a dir with no active run.
- [ ] **Step 2–4:** Implement; wire the sweep into `create_app()` startup and the existing reaper daemon.
- [ ] **Step 5: Commit** — `feat(media): batch-run route, per-batch delete, run lifecycle + sweep`.

---

## Task 9: Filename scan route (`blueprints/media.py`)

- `POST /media/scan` (JSON): `{categories: {youtube_video: [names], shorts: [...], podcast: [...], thumbnails: [...], email_thumbnails: [...]}}` → returns `{dates: {iso: {category: [names]}}}` using `file_scanner.parse_names`, plus, for each iso date, the spreadsheet metadata (from the cached sheet + session mapping) so the dashboard can show what matched.

**Files:** Modify `blueprints/media.py`; Test: `tests/test_media_scan.py`.

- [ ] **Step 1: Failing test:** post category name-lists, assert the date→category→names structure; with a mapped cached sheet, assert metadata (title/transcript) is attached per date.
- [ ] **Step 2–4:** Implement (compose Task 2 + Task 3/5). 
- [ ] **Step 5: Commit** — `feat(media): /media/scan filename→date matching with sheet metadata`.

---

## Phase B — Frontend & integration (manual verification; concrete specs)

> These can't be unit-tested headlessly. Each task gives concrete markup/JS and explicit acceptance criteria, verified in a browser against the VPS.

## Task 10: Dashboard — pickers, spreadsheet, column mapping (`templates/index.html`)

**Files:** Read `templates/index.html` + `blueprints/scan.py` index route first. Modify `templates/index.html` (and the index route to stop requiring server dirs).

- [ ] **Step 1:** Add five `<input type="file" webkitdirectory directory multiple>` pickers (YouTube video, Shorts, Podcast, Thumbnails, Email thumbnails), a single spreadsheet `<input type="file" accept=".xlsx">`, and — after the sheet uploads — a sheet selector + a column-mapping form (date, youtube_title, podcast_title, episode_title, description, vista_caption, tags, passage, scripture, prayer, topic, **transcript**). Match `base.html` styling.
- [ ] **Step 2:** On spreadsheet pick → `POST /media/spreadsheet` → populate sheet/column selects → mapping `POST /media/mapping`.
- [ ] **Acceptance:** picking folders + a sheet shows the mapping UI; mapping persists across a page reload within the session; no server directory inputs remain.
- [ ] **Step 3: Commit** — `feat(ui): dashboard folder pickers + spreadsheet + column mapping`.

## Task 11: Client scan + matched-date selection (`static/js/dld_pipeline.js`)

**Files:** Create `static/js/dld_pipeline.js`; include from `index.html`.

- [ ] Harvest filenames from each picker's `FileList` (store the `File` objects in a JS map keyed by category→filename for later upload). `POST /media/scan` with the name lists → render matched dates per category with the sheet metadata → user selects dates + platforms (reuse existing platform toggles).
- [ ] **Acceptance:** matched dates render per category; selecting dates + platforms enables an "Upload" action; junk files (`.DS_Store`) don't appear.
- [ ] Commit — `feat(ui): browser scan + matched-date selection`.

## Task 12: Client batch orchestration (chunked upload + SSE) (`static/js/dld_pipeline.js`)

- [ ] On Upload: `POST /media/run/init` → `{run_id}`. Chunk the selected dates into batches of 4. Per batch: for each distinct `File` needed, `POST /media/file/new` → `file_id`, then `File.slice()` into ≤90 MB chunks → sequential `POST /media/upload/chunk`; once all batch files report `complete`, `POST /media/batch/run` and consume `/upload/stream` SSE (reuse the existing progress UI). On batch `done`, mark those dates complete and proceed to the next batch. At the end, `POST /media/run/finish`.
- [ ] Show a persistent **"Keep this tab open until the upload finishes"** warning during a run; `beforeunload` confirm. On reload, already-succeeded dates show as done (server idempotent skip makes re-running safe).
- [ ] **Acceptance (manual, VPS):** a 5-date selection uploads in 2 batches; disk under `/data/uploads` never holds more than one batch; temp dirs vanish after each batch and at the end; a deliberately-closed tab mid-run, re-run, does **not** double-upload an already-succeeded date.
- [ ] Commit — `feat(ui): browser-orchestrated chunked batch upload`.

## Task 13: Settings cleanup (`blueprints/settings.py`, `templates/settings.html`)

- [ ] Remove the media-directory inputs + the per-platform browse buttons + the Whisper section. Keep: schedules, platforms, footers, secrets, Connect, change-password, LLM status. Remove the `/browse`, `/scan-config`, `/validate-path` routes (Task 14) once nothing references them.
- [ ] **Acceptance:** Settings renders with no directory/whisper UI; nothing 500s.
- [ ] Commit — `refactor(settings): remove directory + whisper UI`.

## Task 14: Remove server-directory scanning (`blueprints/scan.py`, `core/config.py`, `config.yaml`)

- [ ] Read `blueprints/scan.py`; remove `/browse`, `/validate-path`, `/scan-config`, the directory-based `/scan`, and the directory-picker modal. Remove `resolved_dirs`, `is_path_allowed`, `allowed_path_roots`, and the `directories`/`sharepoint_docx`/`excel_mapping` blocks from `config.yaml` and `core/config.py` (grep for all references first; the new flow doesn't use them).
- [ ] Run full suite; fix/adjust any tests that referenced removed pieces (e.g. directory-scan tests) — delete or rewrite to the new `/media/scan` contract.
- [ ] **Acceptance:** suite green; no dead references to `directories.base`.
- [ ] Commit — `refactor: remove server-side directory model + config defaults`.

---

## Phase C — Deploy & verify

## Task 15: Deploy + end-to-end VPS verification

- [ ] Update `deploy/Dockerfile` if ffmpeg dropped; ensure `DLD_UPLOAD_TMP` defaults to `/data/uploads` (on the `dld-data` volume) so temp files share the volume and the free-space check is meaningful.
- [ ] `cd ~/DailyLifeDistributor && git pull && cd deploy && docker compose up -d --build`.
- [ ] **Manual acceptance (record results in the commit/PR):**
  1. Pick 5 category folders + spreadsheet, map columns (incl. transcript), scan → matched dates show.
  2. Title suggestions on a date use the transcript column (Ollama), no upload.
  3. Upload a few dates → watch batches of 4, `du -sh /data/uploads` stays bounded, temp dirs vanish per batch.
  4. A platform success is recorded; re-running the same date **skips** it (idempotent).
  5. Disk-low path: simulate by lowering the margin or a huge declared total → batch rejected with an actionable message.
- [ ] Commit — `chore(deploy): browser-streaming pipeline live config`.

## Task 16: Docs

- [ ] Update `CLAUDE.md` + `README.md`: the new browser-streaming workflow, no server directories, transcript-column titles, chunked batch uploads, `DLD_UPLOAD_TMP`. Note the per-session, one-run-at-a-time model and the keep-tab-open requirement.
- [ ] Commit — `docs: document the browser-streaming media pipeline`.

---

## Self-Review / Sequencing Notes

- **Order rationale:** Tasks 1–9 are backend and unit-testable in isolation; the UI (10–12) builds on the stable endpoints; cleanup (13–14) happens once the new path works; deploy/docs last.
- **Verify-during-execution flags:** `core/file_scanner.py` date helper (Task 2), `core/excel_parser.py` API (Task 3), `blueprints/review.py` title path (Task 4), `core/upload_jobs.py` `_run_upload_job`/`_upload_one`/email-wait + `ReviewEntry` path fields (Task 7), `app.py` blueprint registration + reaper daemon (Tasks 5/8). Read each before editing.
- **Cloudflare:** chunk cap is ≤95 MB to stay under the ~100 MB proxied-body limit; if uploads still fail through the Tunnel, drop to 50 MB.
- **Idempotency is load-bearing** (Task 7): it's what makes the no-resume / re-run model safe. Don't defer it.
- **Disk guarantee** comes from per-batch delete + `finally` cleanup + startup/idle orphan sweep + free-space precheck — all four are required.
