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

**Hybrid agent (opt-in):**
- `HYBRID_AGENT_ENABLED` — set to `"true"` on the server (`.env` for the
  Flask app, plus the docker-compose env block on the VPS) to enable the
  agent path. The dashboard's Agent chip + `?path=agent` query on
  `/media/batch/run` are no-ops without this flag, so unsetting it is a
  safe full-disable switch even with paired devices online.

## Architecture Overview

This is a single-file Flask app (`app.py`) backed by `core/` modules and four uploaders (YouTube, SimpleCast, Vista Social, Rock — Rock itself spans the Daily Experience orchestrator + the Rock Email sub-uploader), with a small SQLite database for session/history persistence.

**User workflow (browser-streaming pipeline — see its own section below):**
1. **Setup** (`/`) — pick per-category media folders + spreadsheet in the browser, map columns, match dates (`/media/scan`), select dates + platforms
2. **Upload** — the browser orchestrates a batched, chunked upload to `/media/*`; per-batch progress streams via SSE at `/upload/stream`
3. **History** (`/history`) — past sessions and per-row upload outcomes loaded from SQLite

(The legacy `/review` and `/confirm` blueprints were removed when the dashboard replaced the old three-page flow.)

**Core modules (`core/`):**
- `media_session.py` — per-run temp dir lifecycle (`RunDir`), the single-active `RunLock`, the orphan sweep, and the free-space precheck for the streaming pipeline
- `file_scanner.py` — `parse_names(filenames)` matches dates from a list of filenames (no filesystem) using multi-format digit extraction (YYMMDD, DDMMYY, DDMMYYYY, YYYYMMDD, MMDD); 6-digit ambiguity surfaces the file under both candidate dates.
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
Dispatch goes through `_dispatch_upload` / `_resolve_youtube_watch_url`.

**Per-platform circuit breaker** (`core/circuit_breaker.py`) — `_dispatch_upload`
guards each platform with a name-keyed breaker (`upload:<platform>`). After
`upload.circuit_breaker.failure_threshold` consecutive **infra** failures
(`SessionExpiredError`, Playwright `TimeoutError`, network/`OSError`) the
platform fails fast for the rest of the run instead of relaunching Chrome and
burning the login timeout on every remaining date — then re-probes after
`recovery_timeout_seconds`. Per-row data failures (result dict with
`success: False`, no exception) are neutral and never trip it. The local LLM
(`core/llm_title_gen.py`) uses the same breaker (`llm:title`) plus one
transient-retry on the completion POST.

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

## Hybrid upload agent (Phase 3)

The hybrid agent is an opt-in, locally-run companion to the hosted web app.
Its job is to take the same per-row upload plan the server builds and run the
bundled uploaders **on the user's own machine** — so YouTube uploads happen
from the home network's outbound bandwidth and Playwright session cookies
(SimpleCast / Vista Social / Rock) live on the user's USB instead of the
VPS volume.

**Web vs. agent path choice.** The dashboard shows an "Agent" chip when
`HYBRID_AGENT_ENABLED=true` is set on the server and a paired device is
currently online. The user picks the path before clicking Upload; the
browser sends `?path=agent` to `/media/batch/run`, the server's
`blueprints/media.py:batch_run` switches branches, and an `agent_dispatch.start`
call replaces the in-process `_run_batch_worker` thread.

**Data flow (one batch):**
1. Browser does the usual chunked upload to `/media/upload/chunk` for the
   run's reassembly handshake (the agent path doesn't consume those files —
   `_release_run` deletes them as soon as the dispatch returns).
2. Server builds a `job_plan` envelope (rows + paths + platform elements +
   credentials snapshot) and pushes it to the paired agent via the relay
   (`core/relay.py` -> `wss://.../agent/socket`).
3. Agent's `agent/main.py:_on_message` receives the `job_plan`, spawns a
   daemon worker thread (so the receive loop stays under Cloudflare's
   ~100s idle timeout), and routes to `agent/dispatch.py:handle_job_plan`.
4. `handle_job_plan` installs the credentials + db shims
   (`agent/secrets_shim.py`, `agent/db_shim.py`) so the bundled uploaders
   (which `from core import secrets_store` / `from core import db`) see
   the envelope's secrets and emit instead of writing to a SQLite file.
5. `agent/run_batch.py:run()` resets the breakers, builds a per-run YT
   state, and dispatches each `(row, platform)` through the same
   uploader code paths the web server uses.
6. Every emitted event frame flows back over the WebSocket to
   `core/relay.py`, then into the per-job SSE queue that the dashboard's
   `/upload/stream` is reading — so the UI looks identical to the web path.
7. `pending_results` + `EventBuffer` (in `agent/dispatch.py`) cover the
   "agent reconnects mid-job" case: success rows accumulate locally and
   replay in the next hello frame; the server applies them idempotently.

**Single-job invariant.** The agent runs one job at a time. A second
`job_plan` arriving while one is in flight is rejected with a synthetic
`error` event (`agent busy with job <prev_id>`); per-job state lives
inside the worker thread.

### Phase 3.5 — HWID-tagged devices + same-network picker

The pair-redeem and dispatch paths grew three concrete signals so users with
two paired agents (laptop + studio) can target the right one each run:

- **HWID-tagged device records.** The agent computes a salted sha256 of
  `py-machineid`'s machine id (`agent/hwid.py`) plus a friendly hostname
  (`agent/hostname.py`, `.local` stripped, length-capped) and sends both
  in the `/agent/pair/redeem` body. `core/devices.py:redeem_pairing_code`
  persists them in two nullable columns (`hwid_hash`, `hostname`); old
  agents that don't send the fields still pair successfully. A
  `find_by_hwid(hash)` helper is exposed for future re-link UX.
- **`agent_ip` capture.** The relay (`core/relay.py`) records the public
  IP each agent connected from at handshake (`agent_ips: device_id → ip`).
  `blueprints/agent.py:_client_ip` resolves the *real* IP via
  `CF-Connecting-IP` → first `X-Forwarded-For` entry → `request.remote_addr`,
  used both at the agent socket and the new browser-side route.
- **`GET /agent/devices/online`.** Session-auth-gated endpoint returns
  one entry per currently-connected agent:
  `{id, name, hostname, hwid_hash_short, last_seen_at, same_network}`.
  `same_network` is `True` when the agent's stored `connect_ip` equals
  the browser's `_client_ip()` (neither side `"unknown"`).
- **Dispatch fallback chain.** `core/agent_dispatch.py:_pick_device` now
  takes optional `device_id` + `browser_ip` and runs:
  1. Explicit `device_id` if it's currently online → win.
  2. Exactly one online device → auto-pick.
  3. Exactly one online device with `connect_ip == browser_ip` →
     same-network win (ambiguous matches fall through).
  4. `most_recently_seen_online()` — the original behavior.
  `NoAgentOnlineError` only when all four yield nothing. The dashboard
  passes `device_id` via `?device_id=` on `/media/batch/run?path=agent`.
- **`whoami_ping` / `whoami_pong`.** Live identity confirmation: the
  browser sends `{type: "whoami_ping", ping_id}` on its `/agent/ws`
  socket; the relay forwards to all agents in the room; each agent's
  `agent/main.py:_on_message` replies with `whoami_pong` carrying
  `{ping_id, device_id, hwid_hash, hostname, protocol_version}`. The
  dashboard chip refreshes its displayed hostname from the pong so
  drift (reinstall, hostname change) appears without re-pairing.
- **UI sticky preference.** `static/js/dld_pipeline.js` stores the
  picker selection in `localStorage["dld:preferred_agent"]`. If the
  stored device is offline at page load and exactly one same-network
  device is online, that device pre-selects.

## Multi-tenant (phases α / β / γ / δ)

The hosted deploy has grown from "shared password" to a full multi-tenant
SaaS: orgs, users, roles, invitations, account recovery, audit log,
two-factor, and per-org YouTube quota. The single-tenant USB build still
works (legacy login enabled via `LEGACY_PASSWORD_ENABLED=true`, only safe
when `HOSTED` is unset — `app.py` refuses to boot the combination).

**New tables (created in `core/db.py:init_db()`):**
- `organizations` — tenant root; `require_2fa` toggles org-wide enforcement
- `users` — username, email, Argon2id `password_hash`, `password_changed_at` (NULL = first-login forced reset), `totp_secret_encrypted`, `totp_enabled`, `email_2fa_enabled`, `program_owner`
- `org_memberships` — `(user_id, org_id, role)` where role ∈ {owner, manager, user}
- `invitations` — pending invites; token-hashed, single-use, 7-day TTL
- `recovery_codes` — pre-generated one-time-use codes (10 per user, hashed at rest)
- `recovery_requests` — user-initiated "I lost my factor" tickets the Owner approves
- `audit_log` — every security-relevant action (login, 2fa change, invite, role change, password reset…)
- `audit_log_archive` — nightly rollover at 03:00 UTC (`core.audit_archive`)
- `email_2fa_codes` — 6-digit codes, 10-min TTL, rate-limited per user
- `login_ip_sightings` — per-user IP history powering "new device sign-in" emails
- `platform_locks` — coarse per-org per-platform mutex so two simultaneous runs in the same org don't tangle Playwright sessions
- `yt_quota_usage` — per-`(org_id, quota_date)` counter, parallel to the global `youtube_quota`

**Routes — program-owner admin:**
- `/admin/organizations`, `/admin/organizations/<id>` — list + per-org dashboard
- `/admin/users` — every user across every org
- `/admin/audit-log` — global audit feed (org-owners get their own at `/settings/audit-log`)

**Routes — per-user account:**
- `/settings/2fa`, `/settings/2fa/enable-totp`, `/settings/2fa/verify-totp`, `/settings/2fa/enable-email`, `/settings/2fa/send-email-code`, `/settings/2fa/disable`, `/settings/2fa/recovery-codes`
- `/settings/security` — sessions list, IP sightings, recent logins
- `/settings/members` — owner/manager: invite, change roles, remove (audit-logged)
- `/settings/audit-log` — org-scoped feed

**Routes — auth + recovery:**
- `/login` then `/login/2fa` (TOTP) or `/login/email-2fa`
- `/login/first-password-set` — forced when `users.password_changed_at IS NULL` (every brand-new + invited user)
- `/recover` (anonymous), `/recover/reset/<token>`, `/recover/admin-approve/<id>` (Owner)

**Routes — invitations + agent download:**
- `/invite/accept/<token>` (GET form + POST submit) — public, no session
- `/download/agent`, `/download/agent/windows`, `/download/agent/macos`, `/download/agent/manifest.json` — stable user-facing URLs; the manifest + signed binaries also flow through `/agent/releases/*` for the auto-updater

**Routes — session + health:**
- `/sessions/status` — JSON heartbeat the dashboard polls so a lapsed session triggers a friendly modal (not a silent 401)
- `/health/details` — operational telemetry (breakers, agents_online, resend_configured, youtube_quota, secret_enc_key_set); always 200, scraped by external monitors

**New env vars:**
- `PROGRAM_OWNER_EMAIL` — first-boot migration creates this user as program owner (paired with `INITIAL_ADMIN_PASSWORD`)
- `LEGACY_PASSWORD_ENABLED` — `"true"` enables the shared-password gate; refuses to combine with `HOSTED=true`
- `RESEND_API_KEY`, `RESEND_FROM_ADDR` — outbound mail (invites, recovery, new-device alerts); `RESEND_API_KEY` empty disables mail (logged as warning)
- `BASE_URL` — public origin used when generating absolute links in outbound emails (default `https://autoalert.pro`)

**Agent GUI + universal2:**
- `agent/gui.py` — bundled Tkinter window for the desktop agent: pair, show status (online / offline / running job), open update log
- `agent/state.py` — durable per-install state (paired token, last device id, settings)
- The Mac build is now a **universal2** binary (`build_agent.py` + the CI matrix) so one download runs on both Apple Silicon and Intel Macs

**First-login forced password change:**
- A brand-new user (created via invite or by the program-owner CLI) has `users.password_changed_at IS NULL`. On login the auth blueprint redirects to `/login/first-password-set`, which is partial-token-gated (the session isn't fully authenticated until the new password lands), then sets `password_changed_at = now()` and proceeds to 2FA if configured.

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
| `core/upload_jobs.py` | `run_batch` parallel runner, SSE events, idempotent skip, per-platform circuit breaker around `_dispatch_upload` |
| `core/circuit_breaker.py` | Thread-safe CLOSED/OPEN/HALF_OPEN breaker + name-keyed registry; used by the upload dispatch and the LLM title generator |
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
| `core/agent_dispatch.py` | Server-side fan-out to the paired agent over the relay: `start` returns a job_id; `register_job` wires the agent's event stream into the existing per-job SSE queue; `NoAgentOnlineError` on no online agent |
| `core/relay.py` | WebSocket relay (browser ↔ agent over `/agent/socket`); message routing, hello/pending_results reconciliation, idle reaper |
| `blueprints/agent.py` | Pairing + WebSocket endpoints (`/agent/pair`, `/agent/socket`, `/agent/status`, etc.); device list management |
| `agent/main.py` | Agent entrypoint: pair → connect → message loop. Spawns `handle_job_plan` on a daemon thread so the receive loop stays responsive; single-job invariant via `_active_job_id` |
| `agent/dispatch.py` | `handle_job_plan`: installs the credential + db shims, resolves local paths, drives `run_batch.run`, ships every event back through the transport. Also hosts `EventBuffer` (bounded reconnect replay) and `PendingResults` (hello-frame success replay) |
| `agent/run_batch.py` | Agent-side `run()`: per-run YT state (`_YtState`), `circuit_breaker.reset_all()` at the top, per-platform dispatch into the bundled uploaders, Rock-Email guard when YT was expected but produced no URL |
| `agent/secrets_shim.py` | In-memory drop-in for `core.secrets_store`. Surfaces `get/set/delete_secret`, `get/set_blob`, `has_secret`, `materialize_blob_to_tempfile`; every mutation emits a `credentials_updated` event so the server stays the source of truth |
| `agent/db_shim.py` | In-memory drop-in for `core.db`. Implements only `record_image_use` (the one call uploaders make); everything else raises `NotImplementedError` to surface new coupling loudly |
| `agent/remote_session.py` | Headless remote-login bridge used when the operator re-authenticates a Playwright session from the web UI while the agent runs the actual login |
| `blueprints/calendar.py` | Calendar view + per-platform schedule refresh endpoints |
| `blueprints/remote_login.py` | VNC-driven Playwright login flow for SimpleCast/Vista/Rock on the hosted deploy |
| `blueprints/history.py` | `/history` rendering from `upload_history` |
| `blueprints/scan.py` | Root dashboard route (`/`) — index + scan UI |
| `blueprints/settings.py` | Settings page, OAuth, llamafile status, env-file editing, devices-management entry |
| `blueprints/upload.py` | `/upload/stream` SSE consumer + `POST /upload/<job_id>/cancel` for agent-path jobs |
| `core/calendar_refresh.py` | Orchestrator that pulls each platform source + reconciles `upload_history` with what's actually scheduled |
| `core/calendar_refresh_view.py` | Read-side projection — merges the refreshed calendar entries into the dashboard's calendar grid |
| `core/refresh/*.py` | Per-platform "what's currently scheduled" sources: `youtube_source`, `simplecast_source`, `rock_source`, `rock_email_source`, `vista_source`, plus `id_extract` helpers |
| `core/image_gatherer.py` | Unsplash / Pexels free-stock-image lookup for Rock spotlight/vista/reflection slots + the credits ledger (`docs/credits_*.txt`) |
| `core/hosted.py` | `is_hosted()` — single source of truth for "are we on the autoalert.pro VPS" branching (env or `/data/HOSTED` marker) |
| `core/quota.py` | YouTube Data API daily-quota tracker (`QUOTA_COSTS`, `DAILY_QUOTA`, per-op recording in the session) |
| `core/vnc.py` | x11vnc / Xvfb + websockify lifecycle for the hosted remote-login flow; provides the noVNC iframe URL |
| `core/remote_login.py` | Top-level coordinator for the remote-login state machine (`idle → launching → awaiting_user → saving → done`) consumed by `blueprints/remote_login.py` |
| `core/remote_login_playwright.py` | Playwright headed-browser driver inside the Xvfb display; writes the resulting `storage_state` to `secrets_store` |
| `uploaders/vista_social_uploader.py` | Vista Social Playwright uploader (no API) — daily-image + caption + per-platform schedules |
| `templates/devices.html` | `/settings/devices` — paired-device list with inline rename + revoke (added in the codebase-completion pass) |
| `core/user_store.py` | Argon2id user CRUD: create, password update, lookup by id/username/email |
| `core/org_store.py` | Organization + membership CRUD; role helpers (list_memberships_for_user, list_members_for_org) |
| `core/permissions.py` | `require_role` / `require_program_owner` / `require_authenticated_json` decorators |
| `core/invitations.py` | Mint/redeem invite tokens (hashed at rest, single-use, 7-day TTL) |
| `core/recovery.py` | Pre-generated recovery codes — generate_recovery_codes, regenerate_codes, consume |
| `core/recovery_request.py` | "I lost my factor" tickets the Owner approves; per-user 1/24h rate limit |
| `core/audit.py` | `write_event(action, actor_user_id, metadata, ip, ua)` — single insert into `audit_log` |
| `core/audit_archive.py` | Nightly rollover at 03:00 UTC: moves rows older than 90 days into `audit_log_archive` |
| `core/email.py` | Resend SDK wrapper; templates (welcome, invite, recovery, new-device, etc.); per-call breaker + retry |
| `core/email_2fa.py` | 6-digit email codes, 10-min TTL, rate-limited per user |
| `core/login_notifications.py` | "Sign-in from a new IP" emails — diffed against `login_ip_sightings` |
| `core/totp.py` | TOTP secret gen / encrypt-at-rest / verify / provisioning URI builder |
| `core/passwords.py` | Argon2id verify + hash, Pwned Passwords check |
| `core/migration_bootstrap.py` | Idempotent first-boot: create PROGRAM_OWNER_EMAIL user + default org from env |
| `core/env_validation.py` | Startup-time guard on required env vars (SECRET_ENC_KEY, etc.) — fail-loud at boot |
| `core/platform_locks.py` | Per-(org, platform) coarse mutex preventing concurrent Playwright sessions for the same login |
| `core/playwright_session.py` | Materialize browser-session blobs from the encrypted store at boot |
| `core/qrcode_render.py` | PNG renderer for the TOTP provisioning QR code |
| `blueprints/admin.py` | Program-owner admin pages: orgs list, per-org detail, all-users, global audit |
| `blueprints/audit.py` | Per-org audit log view (`/settings/audit-log`) |
| `blueprints/twofa.py` | Per-user 2FA management: enable/disable TOTP + email, recovery-codes, send-email-code |
| `blueprints/recovery.py` | `/recover` (anonymous), reset form, owner approval; rate-limited |
| `blueprints/invitations.py` | Mint invite, public accept GET/POST; audit hooks for sent/revoked |
| `blueprints/members.py` | Org member list, role change, remove |
| `blueprints/download.py` | Stable user-facing agent download URLs (Windows .exe, macOS universal2) |
| `agent/gui.py` | Bundled Tkinter window: pair status, online indicator, current job, update log |
| `agent/state.py` | Durable per-install state (paired token, device id, settings) |

## Device management + cancel UX (codebase-completion pass)

Three UX gaps the audit caught and this PR closes:

- **Device management UI** — `GET /settings/devices` (in
  `blueprints/settings.py`) renders `templates/devices.html`, showing every
  paired agent (active + revoked) with inline rename + revoke buttons.
  Rename POSTs to `POST /agent/devices/<id>/name` (validates 1..64 chars),
  revoke POSTs to the existing `POST /agent/devices/<id>/revoke`.
  `core.devices.set_device_name` is the underlying helper; hostname stays
  immutable (it's agent-reported), only the user-friendly `name` column
  changes.

- **Re-link UX** — `blueprints/agent.pair_redeem` now consults
  `devices.find_by_hwid(hwid_hash)` before creating a new row. If a prior
  non-revoked device matches the HWID (i.e. the agent reinstalled on the
  same hardware), the old row is revoked, its friendly name carries over to
  the new row, and the response includes `{relinked: true, previous_name}`.
  The agent (`agent/main.py`) logs "Re-linked to <name>" instead of
  "Paired". The consent gate is unchanged — the user still types the
  pairing code.

- **Cooperative cancel** — `POST /upload/<job_id>/cancel` sends a
  `cancel_job` frame to the agent that owns the job. In-flight uploads
  finish; pending platform dispatches emit `error_type=cancelled` events
  and skip. The dashboard surfaces a Cancel button below the upload
  progress (agent-path only — web-only-path cancel is a future addition).
  Wire types are documented in
  `docs/superpowers/specs/2026-05-22-hybrid-upload-agent-phase3-design.md`.