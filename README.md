# DailyLifeDistributor

A local, single-operator Flask app that takes **one day's worth of media** —
sitting in a folder, described by an Excel planning sheet — and publishes it to
every platform a daily-devotional ("Daily Life") program needs, in one guided
workflow. You pick the dates, review the titles/descriptions/schedules it
pre-fills, confirm, and it uploads to all enabled platforms in parallel while
streaming live progress.

Everything runs on the operator's machine. There is **no server-side
component**, no account, and nothing leaves the machine except the uploads
themselves. State lives in a SQLite file next to the app (designed to travel on
the same USB drive as the media).

---

## What it can do (capabilities)

For each calendar **date** you select, it can publish to any combination of
these six destinations. Each is independently toggleable, both globally
(`config.yaml → platforms`) and per-element on the Review page:

| Platform | What gets published | How it uploads |
|---|---|---|
| **YouTube Video** | Full-length (horizontal) video + thumbnail, title, description, tags, scheduled publish time | YouTube Data API v3 (OAuth2) |
| **YouTube Shorts** | Vertical short + thumbnail, title, description, tags, schedule | YouTube Data API v3 (OAuth2) |
| **SimpleCast** | Podcast episode (audio) + summary, notes, optional scheduled publish | Browser automation (Playwright drives Chrome) |
| **Vista Social** | Social post with the video + caption + schedule | Browser automation |
| **Rock (Daily Experience)** | In-app devotional: a parent item plus Spotlight / Vista / Reflection children, with an Unsplash image | Browser automation |
| **Rock Email** | "Daily Life {date}" email/SMS broadcast draft with the day's description, the horizontal YouTube link, and a play-button thumbnail | Browser automation |

Additional capabilities:

- **Automatic file discovery.** Scans your media folders and matches files to
  dates by parsing many filename date formats (YYMMDD, DDMMYY, DDMMYYYY,
  YYYYMMDD, MMDD). Ambiguous 6-digit dates are surfaced so you can pick the
  right interpretation.
- **Excel-driven metadata.** Reads titles, descriptions, tags, scripture,
  prayer, etc. from a planning spreadsheet using a column mapping you configure
  in the UI.
- **AI title suggestions for Shorts.** Transcribes the first ~30 s of the
  media (faster-whisper) and asks a locally-running LLM (llamafile / llama3.2)
  to suggest titles. Fully offline — no cloud LLM.
- **Parallel uploads with live progress.** All platforms for all selected
  dates run on a thread pool; per-row progress (bytes, processing phase,
  success/error) streams to the browser via Server-Sent Events.
- **Cross-platform sequencing.** The Rock Email row for a date waits for that
  date's YouTube Video upload to finish so it can embed the real watch URL.
- **History & resume.** Every attempt is recorded in SQLite; an interrupted
  session can be resumed.
- **Description footers.** Per-platform standing footers are appended
  automatically.

---

## Requirements

| Plain-Python run (any OS) | Mac production launch (`launch_mac.command`) |
|---|---|
| Python **3.11+** | Bundled Python in `bin/python_arm/` or `bin/python_intel/` |
| `pip install -r requirements.txt` | Same, installed into the bundled Python |
| **ffmpeg** on PATH (for transcription) | Bundled `bin/ffmpeg` (falls back to system) |
| **Google Chrome** (for SimpleCast / Vista / Rock) | Same |
| *Optional:* a local LLM for Shorts title ideas | Bundled `bin/llamafile` (auto-started) |

> The bundled `bin/` directory (~3 GB of binaries, runtimes, and model weights)
> is **not** in git — see `bin/README.md` for exactly what to put there. You
> only need it for the Mac launch script. For a plain `python app.py` run you
> just need the table's left column.

The Playwright **pip package** is enough — we drive the system Chrome via
`channel='chrome'`, so you do **not** need to run `playwright install`.

---

## Quick start

```bash
# Mac (primary, production launch path)
./launch_mac.command

# Any platform with Python 3.11+
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:8080**.

`launch_mac.command` auto-detects CPU architecture, selects the bundled Python,
starts `bin/llamafile` for title suggestions on port 8081, and starts Flask on
port 8080. (It binds Flask to `0.0.0.0` for LAN convenience, but the app
rejects every non-loopback request — see [Security model](#security-model).)

---

## First-run setup

Do these once. **2–4 are only needed for the platforms you actually enable.**

### 1. Point the app at your files

Edit `config.yaml` (or use the **Settings** page in the running app — see
below) so it knows where your media and planning sheet live:

```yaml
directories:
  base:            /path/to/DailyLife
  youtube_video:   /path/to/DailyLife/YouTube
  youtube_shorts:  /path/to/DailyLife/Shorts
  podcast:         /path/to/DailyLife/Podcast
  thumbnails:      /path/to/DailyLife/Thumbnails
  email_thumbnails: /path/to/DailyLife/EmailThumbnails   # play-button variant
sharepoint_docx:   /path/to/DailyLife/Daily Life Content Publication.xlsx
```

Then map your spreadsheet's columns to metadata fields. The easiest way is the
**Settings → Excel mapping** UI, which lets you pick the sheet and preview
columns; it writes the `excel_mapping` block of `config.yaml` for you.

### 2. YouTube OAuth credentials *(YouTube Video / Shorts)*

In the Google Cloud Console, create an **OAuth client (Desktop app)** with the
YouTube Data API v3 enabled, download the JSON, and save it as
`client_secrets.json` in the project root (or set
`YOUTUBE_CLIENT_SECRETS_PATH`). The first YouTube upload — or the **Authorize**
button on the Settings page — opens a browser consent flow; the resulting
refresh token is saved to `token.json` and reused afterward.

### 3. Browser-automation logins *(SimpleCast / Vista Social / Rock)*

These have no API — they drive the real dashboards. The **first** time you use
each, a Chrome window opens on that service's login page and waits up to
`*_LOGIN_TIMEOUT` seconds (default 300) for you to log in by hand. On success
the session cookies are saved (`simplecast_session.json`,
`vista_social_session.json`, `rock_session.json`) and reused on later runs. You
can also trigger or clear each login from the **Settings** page.

### 4. Google Chrome

Playwright uses your installed Chrome (`channel='chrome'`). If it's in a
non-standard location, set `SIMPLECAST_CHROME_PATH` (or the per-service
equivalent) to the binary.

---

## The app, page by page

| Page | Route | What you do there |
|---|---|---|
| **Login** | `/login` | Enter the shared password to begin a session. |
| **Index** | `/` | Scans the media folders, lists available dates; pick dates + platforms to publish. |
| **Calendar** | `/calendar` | Calendar view of what's already scheduled/published across sources; refresh from YouTube/Rock. |
| **Review** | `/review` | Per-date editing: titles, descriptions, tags, schedules. Generate AI Shorts-title suggestions here. Toggle individual elements (thumbnail, description, schedule…) per platform. |
| **Confirm** | `/confirm` | Final summary of exactly what will be uploaded where. |
| **Upload** | `/upload` | Kicks off parallel uploads; a live progress view streams over SSE. |
| **History** | `/history` | Past sessions and per-platform outcomes (success/URL/error); resume an interrupted session. |
| **Settings** | `/settings` | Override directory paths at runtime, run/clear each service login, authorize YouTube, configure the Excel column mapping, download the Whisper model, check llamafile status, manage encrypted secrets. |

**Typical run:** open `/` → select dates and platforms → **Review** each date
and tidy the metadata → **Confirm** → **Upload** and watch progress → check
**History** if anything failed and retry.

---

## Configuration files

| File | Purpose | In git? |
|------|---------|---------|
| `config.yaml` | Directory paths, per-platform schedules + on/off toggles, default element toggles, Excel column mapping, LLM/Whisper settings, `upload.max_workers` | yes |
| `.env` | Secrets and Playwright/runtime knobs (see below) | no (gitignored) |
| `client_secrets.json` | Google OAuth client you provide | no (migrated to encrypted store on first boot) |
| `token.json` | YouTube refresh token (auto-generated) | no (migrated to encrypted store) |
| `*_session.json` | Saved Playwright browser sessions (auto-generated) | no (migrated to encrypted store) |
| `state.db` | SQLite — `sessions`, `upload_history`, `image_history`, `external_calendar_items` | no |

### `config.yaml` highlights

- `platforms` — global on/off per platform. Defaults: YouTube Video, YouTube
  Shorts, and SimpleCast **on**; Vista Social, Rock, and Rock Email **off**
  (opt-in).
- `scheduling` — default publish time per platform and the `timezone` used for
  scheduling and YouTube quota reset.
- `defaults.elements` — which sub-elements (thumbnail / title / description /
  tags / schedule, plus Rock children) are active by default; overridable
  per-date on the Review page.
- `description_footers` — text appended to descriptions per platform.
- `excel_mapping` — sheet name + which spreadsheet column feeds each field.
- `upload.max_workers` — parallel upload thread-pool size (default 4).

### Env var reference (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `FLASK_SECRET_KEY` | random per process | Signs the Flask session cookie. **Set this** or sessions reset on every restart. |
| `FLASK_PORT` | `8080` | Override the bind port. |
| `FLASK_DEBUG` | `false` | Enable Flask's debug reloader. |
| `YOUTUBE_CLIENT_SECRETS_PATH` | `./client_secrets.json` | Path override for the OAuth client JSON. |
| `SIMPLECAST_UPLOAD_URL` | show-scoped default | Override the SimpleCast new-episode URL. |
| `SIMPLECAST_HEADLESS` | `false` | `true` to hide Chrome on subsequent runs (first login is always headed). |
| `SIMPLECAST_LOGIN_TIMEOUT` | `300` | Seconds to wait for first-run manual login. |
| `SIMPLECAST_CHROME_PATH` | (auto) | Full path to Chrome if `channel='chrome'` can't find it. |
| `VISTA_SOCIAL_*` / `ROCK_*` | parallel | Same headless / login-timeout / chrome-path knobs for the other Playwright uploaders. |
| `SECRET_ENC_KEY` | (required) | Fernet master key for the encrypted secret store. The app refuses to start without it. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `INITIAL_ADMIN_PASSWORD` | (first boot) | Seeds the shared login password on first start; change it later in Settings. |
| `ALLOWED_HOSTS` | (unset) | Comma-separated hostnames the app accepts (your VPS domain). Unset = no host restriction (local dev). |
| `SESSION_COOKIE_SECURE` | `true` | Whether the session cookie requires HTTPS. **Set `false` for local `python app.py` over plain http**, or login will appear to succeed but every next request bounces back to the login page. |

---

## Health & on-call

- **`/health`** — JSON probe for `state.db` writability, llamafile (port 8081),
  and the Chrome path. Returns HTTP 503 if anything is down. Curl this first
  when something is wrong.
- **`logs/daily_life.log`** — rotating log file (5 MB × 5 backups).

### Recovery runbook

| Symptom | What's wrong | Fix |
|---|---|---|
| App fails to launch, port-conflict message on stderr | Stale Flask or another local server holding 8080/8081 | Kill the process or set `FLASK_PORT` |
| App refuses to start: "SECRET_ENC_KEY not set" | Missing encryption key | Generate via `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and set in `.env` |
| Login page loops forever, or you're logged out after every request | `SESSION_COOKIE_SECURE` is `true` but you're on plain http | Set `SESSION_COOKIE_SECURE=false` in `.env` for local development |
| Title suggestions never appear | llamafile crashed or never started | Check `/health`, re-run the launch script, or start `bin/llamafile` manually |
| `Failed to initialize state.db ...` on startup | `state.db` corrupt (USB unplug mid-write, `kill -9` mid-commit) | Back up the file, delete it, restart — a fresh schema is created |
| SimpleCast/Vista/Rock: "still on a login page after login" | Saved session is broken | Delete the matching `*_session.json` and retry — first-run login fires again |
| Headless run hangs and eventually errors | Saved Playwright session expired; uploader relaunched headed but no human typed | Re-run interactively and complete the login prompt |
| YouTube upload says "needs re-auth" | Refresh token revoked | Delete `token.json` and retry; consent flow opens in browser |
| Transcription / title gen errors about a missing model | Whisper model not downloaded, or ffmpeg missing | Download the Whisper model from Settings; ensure ffmpeg is on PATH |

### `state.db` schema

Managed by `core/db.py:init_db()`:

- `sessions` — workflow-state JSON + label + status; one row per run, used to
  resume after a crash.
- `upload_history` — one row per platform attempt: `success`, `url`,
  `scheduled_time`, `error`. Powers the History page.
- `image_history` — Unsplash image attribution log for the Rock Vistas
  integration.
- `external_calendar_items` — cached calendar items pulled from YouTube/Rock
  for the Calendar page.

Inspect with:

```bash
sqlite3 state.db ".schema"
```

### YouTube quota

`QUOTA_COSTS` in `app.py` tracks estimated API units per operation (video
upload = 100, thumbnail = 50, etc.) against a `DAILY_QUOTA` of 10,000. Counters
live in the Flask session and reset on the calendar day in
`scheduling.timezone`.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Integration tests in `tests/integration/` hit live network sources and are
skipped by default; opt in with the env vars documented in their docstrings.

---

## Security model

- Flask binds to `0.0.0.0` from the launch script for LAN convenience, but
  `app.py` rejects every non-loopback request in `before_request` (HTTP 403).
  Functionally local-only.
- **DNS-rebinding defense:** the `Host` header must name `localhost` or
  `127.0.0.1`.
- **CSRF:** state-changing requests must be same-origin (`Sec-Fetch-Site`) or
  carry an `Origin`/`Referer` matching the host.
- The `*_session.json` files are gitignored but contain dashboard cookies. If
  you back up the USB to cloud storage, those credentials travel with it —
  scope the backup destination accordingly.

---

## Architecture (pointer)

Single Flask app (`app.py`) backed by `core/` (scanning, Excel parsing,
session state, transcription, LLM, SQLite, encryption), `blueprints/` (the routes/pages
above), and `uploaders/` (YouTube, SimpleCast, Vista Social, Rock). **`CLAUDE.md`
is the full engineering reference**; this README is the operator/first-timer
guide.
