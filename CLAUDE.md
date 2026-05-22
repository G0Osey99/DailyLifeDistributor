# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

**Mac (primary launch method):**
```bash
./launch_mac.command
```
This script auto-detects architecture, selects the bundled Python from `bin/python_arm/` or `bin/python_intel/`, starts llamafile for LLM title generation, and launches Flask on port 8080.

**Manual (any platform with Python 3.11+):**
```bash
pip install -r requirements.txt
python app.py
```
The app opens at `http://localhost:8080` with a login gate (session cookie, HttpOnly/SameSite=Lax). Access is controlled by a shared password set via `INITIAL_ADMIN_PASSWORD` on first boot.

**Dependencies:**
- `bin/llamafile` — bundled LLM server (llama3.2) for title suggestions; started by the launch script, listens on port 8081 (the hosted deploy uses Ollama instead via `LLM_BASE_URL`)
- `bin/python_arm/` / `bin/python_intel/` — bundled Python environments (not tracked in git)
- **Playwright + Google Chrome** — required for the SimpleCast uploader. The `playwright` pip package is enough; we drive the system Chrome via `channel=''chrome''`, so `playwright install` is NOT needed.
- Whisper/ffmpeg are **gone** — title suggestions read a mapped transcript column from the spreadsheet (see the browser-streaming pipeline below), not transcribed audio.

## Configuration

`config.yaml` is the central config file. Key sections:
- `scheduling` — default publish times per platform, timezone
- `platforms` — enable/disable YouTube Video, YouTube Shorts, Simplecast, Rock, Rock Email, Vista Social globally
- `defaults.elements` — per-element upload toggles (thumbnail, title, description, tags, schedule)
- `description_footers` — appended to descriptions per platform
- `llm` — model, number of title suggestions, backend, port
- `upload.max_workers` — thread-pool size for parallel platform uploads (default 4)

Media folders, the planning spreadsheet, and the column mapping are **not** in
`config.yaml` — they are picked per browser session on the dashboard and the
mapping lives in the Flask session (see the browser-streaming pipeline below).

`.env` holds secrets and runtime knobs:
- `FLASK_SECRET_KEY`
- `DLD_UPLOAD_TMP` — where per-run media temp dirs are reassembled (defaults to `/data/uploads` when `/data` exists, else a repo-local `.uploads`)
- `YOUTUBE_CLIENT_SECRETS_PATH` (optional override)
- SimpleCast (all optional — see `uploaders/simplecast_uploader.py` docstring):
  - `SIMPLECAST_UPLOAD_URL` — override the show-scoped new-episode URL
  - `SIMPLECAST_HEADLESS` — `"true"` to hide the Chrome window once a session is cached (first-run login is always headed)
  - `SIMPLECAST_LOGIN_TIMEOUT` — seconds to wait for first-run manual login (default 300)
  - `SIMPLECAST_CHROME_PATH` — full path to Chrome if not in a standard location

The legacy `SIMPLECAST_API_KEY` / `SIMPLECAST_SHOW_ID` variables are no longer used — the REST integration was retired (see SimpleCast section below).

## Architecture Overview

This is a single-file Flask app (`app.py`) backed by `core/` modules and two uploaders, with a small SQLite database for session/history persistence.

**User workflow (browser-streaming pipeline — see its own section below):**
1. **Setup** (`/`) — pick per-category media folders + spreadsheet in the browser, map columns, match dates (`/media/scan`), select dates + platforms
2. **Upload** — the browser orchestrates a batched, chunked upload to `/media/*`; per-batch progress streams via SSE at `/upload/stream`
3. **History** (`/history`) — past sessions and per-row upload outcomes loaded from SQLite

(The legacy `/review` and `/confirm` blueprints still exist but the dashboard is the primary path.)

**Core modules (`core/`):**
- `media_session.py` — per-run temp dir lifecycle (`RunDir`), the single-active `RunLock`, the orphan sweep, and the free-space precheck for the streaming pipeline
- `file_scanner.py` — `parse_names(filenames)` matches dates from a list of filenames (no filesystem) using multi-format digit extraction (YYMMDD, DDMMYY, DDMMYYYY, YYYYMMDD, MMDD); 6-digit ambiguity surfaces the file under both candidate dates. (The legacy `FileScanner` directory-scan class remains but is no longer the primary path.)
- `excel_parser.py` — `parse_spreadsheet(path, mapping)` reads an uploaded `.xlsx` at an arbitrary path with a per-session column mapping (incl. `transcript_column`)
- `session_state.py` — in-memory singleton (`session`); `ReviewEntry` dataclass holds all per-date fields (incl. `transcript`); `UploadElements` controls which upload components are active per platform
- `llm_title_gen.py` — calls the LLM''s OpenAI-compatible API to generate title suggestions from the mapped transcript text; results cached by text hash
- `db.py` — thin SQLite wrapper (`state.db`). Tables incl. `sessions` and `upload_history`; `has_successful_upload()` powers the idempotent re-run skip.

**Uploaders (`uploaders/`):**
- `youtube_uploader.py` — YouTube Data API v3; OAuth2 via `client_secrets.json` / `token.json`; resumable upload in 5MB chunks; sets thumbnail after upload; emits both byte-level progress and processing-phase events; respects `UploadElements` flags for each component
- `simplecast_uploader.py` — **Playwright-driven browser automation** (see below). No REST API.

**Upload flow:**
`/upload` snapshots `session.entries` and the summary, then spawns a background thread (`_run_upload_job`) which uses a `ThreadPoolExecutor` (`config.upload.max_workers`, default 4) to run all platform uploads in parallel. Per-row events (`start`, `upload_progress`, `phase_change`, `processing_start`/`processing_done`, `success`, `error`, `skip`, `done`) are pushed onto a per-job `queue.Queue` and streamed to the browser via SSE at `/upload/stream?job_id=...`. The job store (`_jobs` dict in `app.py`) maps job IDs to queues. Each completed row is also written to `upload_history` via `core.db.record_upload`, and the session is marked completed in the `sessions` table when the job finishes.

**YouTube quota tracking:**
`QUOTA_COSTS` in `app.py` tracks estimated API units per operation (video upload=100, thumbnail=50, etc.) against a `DAILY_QUOTA` of 10,000 units, stored in the Flask session.

## Browser-streaming media pipeline (`blueprints/media.py`, `static/js/dld_pipeline.js`)

Replaces the old server-side directory-scan model. Media lives on the user's
computer; the browser streams it to the VPS just-in-time, in batches, so the
~80 GB box holds at most a few dates' files at once.

**Per browser session:** the column mapping + the cached spreadsheet are keyed
to a `media_sid` minted into the Flask session cookie. **Per process:** a single
`RunLock` (one upload run at a time) and per-run temp dirs under
`DLD_UPLOAD_TMP` (`/data/uploads` on the VPS).

**Flow (all routes auth-gated):**
1. `POST /media/spreadsheet` (multipart) caches the `.xlsx` per session → returns sheet names; `GET /media/spreadsheet/columns?sheet=` lists columns; `POST /media/mapping` stores the column mapping in `flask.session["excel_mapping"]`.
2. `POST /media/scan` (`{categories: {cat: [filenames]}}`) → `parse_names` groups by date + attaches `parse_spreadsheet` metadata per matched date.
3. Upload: `POST /media/run/init` acquires the run lock + a `RunDir` (409 if busy). The browser chunks the selected dates into batches of 4. Per batch: `POST /media/file/new` issues an opaque server uuid `file_id` per distinct physical file, then `File.slice()` → ≤95 MB chunks → sequential `POST /media/upload/chunk` (append-in-order, per-chunk + free-space caps). Once every file is `complete`, `POST /media/batch/run` (reassembly handshake; 409 otherwise) runs `core.upload_jobs.run_batch` against the temp paths, streams progress over the existing `/upload/stream` SSE, and **deletes the batch's temp files** on completion. `POST /media/run/finish` releases the lock + cleans the run dir.

**`core.upload_jobs.run_batch`** points each `ReviewEntry`'s path fields at the
batch's temp files, dedupes by physical file, **idempotently skips** any
`(date, platform)` already recorded `success` in `upload_history`
(`db.has_successful_upload`), and preserves the email-waits-for-YouTube ordering.
Shared `_dispatch_upload` / `_resolve_youtube_watch_url` helpers are reused by
the legacy whole-session `run_upload_job`.

**Disk guarantee** = per-batch delete + run-level `finally` cleanup + an orphan
sweep (`media_session.sweep_orphans`) wired into `create_app()` startup and the
remote-login idle reaper + the free-space precheck. **Idempotent skip** is what
makes the no-resume / re-run model safe — the dashboard re-renders completed
dates as done so a re-run only sends the rest.

## SimpleCast uploader (Playwright rewrite)

The previous SimpleCast integration used the REST API (`SIMPLECAST_API_KEY` + presigned-URL audio PUT). It has been **replaced entirely** with a Playwright script that drives the SimpleCast dashboard the same way a human would. The REST API is no longer touched.

**Why the change:** the REST API was unreliable for the show''s setup (audio-processing/encoding edge cases, scheduling quirks). Driving the dashboard is more robust and matches what the user does manually.

**How it works (`uploaders/simplecast_uploader.py`):**
1. **Session storage.** Cookies + local storage for the SimpleCast dashboard live in `simplecast_session.json` at the project root (next to `app.py`, on the USB). The session travels with the USB drive — no machine-local Chrome profile is touched.
2. **Launch.** Playwright launches the system Google Chrome (`channel=''chrome''`, or `SIMPLECAST_CHROME_PATH` if set). No `playwright install` step is required.
3. **Auth.**
   - If `simplecast_session.json` exists, it''s loaded as `storage_state`. If headless is enabled and the session has expired, the uploader transparently relaunches headed so the user can re-authenticate.
   - If the session is missing or invalid, a headed Chrome window opens to the SimpleCast login URL and waits up to `SIMPLECAST_LOGIN_TIMEOUT` seconds for the user to log in manually. On success, `context.storage_state(path=...)` writes the session file for future runs.
4. **New-episode form.** Navigates to the show-scoped `/episodes/new` URL (`_DEFAULT_UPLOAD_URL`, overridable). Fills:
   - Title (`#form-input-title`)
   - Episode Summary (`#form-input-description`)
   - Episode Notes — CKEditor 5 contenteditable (`.ck-editor__editable`); we focus, select-all, delete, then type, because setting `innerHTML` desyncs CKEditor''s model.
   - Audio file via the hidden `input[type=file]`, with a fallback to clicking the visible "Browse" link and using `expect_file_chooser`.
5. **Save.** Waits for the drag-drop prompt to detach (signals audio upload/processing finished), polls until the Save button is enabled (up to `_UPLOAD_TIMEOUT` = 10 minutes), then clicks Save and waits for the URL to leave `/episodes/new`. The episode is now a draft.
6. **Schedule (optional).** If `entry.podcast_schedule_dt` is set and `sc_schedule` is enabled:
   - Converts the datetime to `America/New_York` (SimpleCast''s display tz).
   - Opens the v-calendar `.timeframe-picker`, computes the month delta from the current header (case-insensitive `%B %Y` parse — header is CSS-uppercased), and clicks prev/next exactly that many times instead of read-and-loop (more reliable on fast machines).
   - Clicks the day cell by stable id selector `.vc-day.id-YYYY-MM-DD`, with an aria-label fallback.
   - Sets the three custom `<li value="...">` time dropdowns (hour 00–11 where 00 displays as 12, minute snapped to a 5-min grid, am/pm).
   - Closes the picker (Escape), clicks the Schedule button once it''s enabled, and confirms the "Are you sure?" dialog by clicking Yes.
7. **Progress callbacks.** `progress_callback(phase)` emits: `launching`, `awaiting_login` (first run only), `navigating`, `filling_form`, `uploading_audio`, `publishing`, `scheduling`, `done`.
8. **Cleanup.** `page.close()` + `browser.close()` in a `finally` block; partial state on errors still leaves the episode as a draft on SimpleCast.

**Selectors and quirks worth remembering** (all defined as `_SEL_*` constants near the top of the file):
- `.timeframe-picker` carries a `disabled` class as its idle state — that is NOT a readiness signal, so don''t wait for it to clear.
- The picker title text is uppercased via CSS, so compare case-insensitively against `target.strftime("%B %Y")`.
- Hour dropdown uses `value="00"` for noon/midnight (displays as 12).
- Minute dropdown is in 5-minute increments only — snap with `(round(minute/5)*5) % 60`.

## Rock "Daily Life" email channel (separate from Daily Experience)

The `uploaders/rock/` package drives **two** different Rock content channels:

1. **Daily Experience** (in-app) — `upload_daily_experience` builds a parent
   plus Spotlight / Vista / Reflection children. (See `orchestrator.py`.)
2. **Daily Life email** (email/SMS broadcast) — `schedule_email`
   (`uploaders/rock/email.py`) queues one email content-channel item per date
   as a **draft** (leaves `Sent = "No"`). This is the `"Rock Email"` platform.

Both share `RockBrowserClient` (Playwright + the `rock_session.json` session).

**Email item fields** (channel GUID `2182c1f3-8f8c-44f3-987f-75a698fe44a7`,
`ContentChannelId=24`, recon via `scripts/rock_email_recon.py`):
- **Title** — `email_title(date)` → "Daily Life {Month} {day}, {year}".
- **Start** — the send date (set via JS on the `dpStart` datepicker, like the
  parent form).
- **Email Message / SMS Message** — `compose_email_message(description,
  existing)`: the day's Excel description prepended **above** the channel's
  standing footer (production: "Here is today's Daily Life:"), which the Add
  form pre-fills. We prepend onto whatever's pre-filled so a footer edit in
  Rock flows through automatically. SMS mirrors Email by default.
- **YouTube Link** — the **horizontal** (non-Shorts) watch URL.
- **Thumbnail** — uploaded from a **separate** media directory
  (`directories.email_thumbnails`), the YouTube-play-button-overlay variant,
  scanned into `ReviewEntry.email_thumbnail_path`. Distinct from the YouTube
  `thumbnails` dir.
- Selectors are by stable attribute-id suffix (`id$="attribute_field_NNNNN"`)
  in `constants.py` (`_SEL_EMAIL_*`).

**"Email after YouTube within a flow, or provided links" rule** — enforced in
`core/upload_jobs.py`. For each date with a non-skipped `"YouTube Video"` row
in the same run, the `"Rock Email"` worker **waits** (up to 30 min) for that
upload's result and uses its `watch?v=` URL. If YouTube Video isn't in the run
for the date, it falls back to `ReviewEntry.youtube_watch_url`; if neither is
present the row errors. (FIFO submission guarantees the video row starts before
its email row, so the wait never deadlocks — it only holds a worker slot.)

The email channel is **opt-in**: `platforms.rock_email` defaults `false`.
Element toggles: `rock_email_enabled`, `rock_email_thumbnail`.

## Key Files

| File | Purpose |
|------|---------|
| `app.py` | App factory, blueprint registration, parallel upload orchestration, SSE streaming, startup orphan sweep |
| `blueprints/media.py` | Browser-streaming pipeline endpoints (`/media/spreadsheet`, `/media/mapping`, `/media/scan`, `/media/run/init`, `/media/file/new`, `/media/upload/chunk`, `/media/batch/run`, `/media/run/finish`) |
| `core/media_session.py` | `RunDir` temp-dir lifecycle, single-active `RunLock`, orphan sweep, free-space precheck |
| `static/js/dld_pipeline.js` | Dashboard client: folder pickers, spreadsheet+mapping, scan, browser-orchestrated chunked batch upload + SSE |
| `templates/index.html` | Setup dashboard (folder pickers, spreadsheet, column mapping, matched dates, upload) |
| `config.yaml` | Runtime configuration (schedules, platforms, `upload.max_workers`) — no directories/spreadsheet/mapping |
| `.env` | Secrets, `DLD_UPLOAD_TMP`, and SimpleCast/Playwright env knobs |
| `client_secrets.json` | Google OAuth2 credentials (download from GCP Console) |
| `token.json` | Auto-generated YouTube OAuth token |
| `simplecast_session.json` | Saved SimpleCast browser session (cookies + local storage); written by Playwright on first successful login |
| `state.db` | SQLite database — `sessions` and `upload_history` tables |
| `core/db.py` | SQLite wrapper used for resume + History page + `has_successful_upload` idempotency |
| `core/upload_jobs.py` | `run_upload_job` (legacy) + `run_batch` (streaming) parallel runners, SSE events, idempotent skip |
| `uploaders/simplecast_uploader.py` | Playwright-based SimpleCast automation (no REST API) |
| `uploaders/rock/email.py` | `schedule_email` — Daily Life email content-channel item (the "Rock Email" platform) |
| `scripts/rock_email_recon.py` | Read-only recon of the email channel's Add form (dumps field ids/selectors) |
| `scripts/migrate_secrets.py` | Idempotent plaintext-to-encrypted migration: auto-runs on first boot, imports env API keys, `client_secrets.json`, `token.json`, `*_session.json` into `secrets` table |
| `blueprints/auth.py` | Login/logout routes, `_require_auth` gate decorator used by all protected routes in `app.py` |
| `core/crypto.py` | Fernet master-key wrapper; fail-closed on missing/invalid key |
| `core/secrets_store.py` | Encrypted KV store backed by `secrets` SQLite table; temp-file materialization for browser sessions |
| `core/auth.py` | Shared-password gate, per-IP lockout, `INITIAL_ADMIN_PASSWORD` bootstrap, password verification |
| `rock_session.json` | Saved Rock browser session (shared by both Rock channels; migrated to encrypted store) |
| `templates/login.html` | Shared password login form |
| `templates/history.html` | History page rendered from `upload_history` |