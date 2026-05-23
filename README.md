# DailyLifeDistributor

A Flask app that takes **one day's worth of media** — described by an Excel
planning sheet — and publishes it to every platform a daily-devotional
("Daily Life") program needs, in one guided workflow. You pick the media
folders and spreadsheet **in your browser**, match dates, then it uploads to
all enabled platforms in parallel while streaming live progress.

**Browser-streaming pipeline.** Media lives on your own computer — nothing is
installed there. You pick per-category folders with the browser's directory
picker; the browser matches filenames to dates against the server, then uploads
each batch of dates (chunked) to the hosted app just-in-time, which consumes
the files for every platform that needs them and deletes them before the next
batch. A small VPS never holds more than a few dates' files at once. State
(sessions, upload history) lives in a SQLite file on the server's data volume.

---

## What it can do (capabilities)

For each calendar **date** you select, it can publish to any combination of
these six destinations. Each is toggleable globally (`config.yaml → platforms`)
and per-run on the dashboard; per-element defaults live in
`config.yaml → defaults.elements`:

| Platform | What gets published | How it uploads |
|---|---|---|
| **YouTube Video** | Full-length (horizontal) video + thumbnail, title, description, tags, scheduled publish time | YouTube Data API v3 (OAuth2) |
| **YouTube Shorts** | Vertical short + thumbnail, title, description, tags, schedule | YouTube Data API v3 (OAuth2) |
| **SimpleCast** | Podcast episode (audio) + summary, notes, optional scheduled publish | Browser automation (Playwright drives Chrome) |
| **Vista Social** | Social post with the video + caption + schedule | Browser automation |
| **Rock (Daily Experience)** | In-app devotional: a parent item plus Spotlight / Vista / Reflection children, with an Unsplash image | Browser automation |
| **Rock Email** | "Daily Life {date}" email/SMS broadcast draft with the day's description, the horizontal YouTube link, and a play-button thumbnail | Browser automation |

Additional capabilities:

- **Browser folder pickers + filename date matching.** You pick a folder per
  media category in the browser; the server matches filenames to dates by
  parsing many formats (YYMMDD, DDMMYY, DDMMYYYY, YYYYMMDD, MMDD). Ambiguous
  6-digit dates surface under both candidate dates.
- **Excel-driven metadata.** You upload your planning spreadsheet once per
  browser session and map its columns (incl. a transcript column) on the
  dashboard; titles, descriptions, tags, scripture, prayer, etc. come from it.
- **AI title suggestions.** Feeds the spreadsheet's mapped **transcript column**
  to a locally-running LLM (Ollama / llama3.2) to suggest titles — no audio
  transcription, no extra upload.
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
| **Google Chrome** (for SimpleCast / Vista / Rock) | Same |
| *Optional:* a local LLM for title ideas | Bundled `bin/llamafile` (auto-started) |

> A modern desktop browser is required for the folder pickers
> (`webkitdirectory`); mobile folder-picking is out of scope.

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
port 8080. (Access is gated by a shared-password login — see [Security model](#security-model). The old loopback-only restriction has been removed so the app can run on a VPS.)

---

## First-run setup

Do these once. **2–4 are only needed for the platforms you actually enable.**

### 1. Pick your files on the dashboard

There is **no directory config** — everything is per browser session. On the
Setup page (`/`):

1. Pick a folder for each media category (Horizontal Video, Vertical Video,
   Podcast, Thumbnails, Email Thumbnails) with the folder pickers.
2. Upload your planning `.xlsx` and map its columns to the metadata fields
   (date, titles, description, tags, scripture, prayer, topic, **transcript**).
   The mapping persists for your browser session.
3. Click **Match dates from folders** to see which dates have media + metadata,
   then select dates + platforms and upload.

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
| **Setup** | `/` | Pick media folders + spreadsheet, map columns, match dates, select dates + platforms, and run the batched upload (progress streams over SSE). Keep the tab open while it runs. |
| **Calendar** | `/calendar` | Calendar view of what's already scheduled/published across sources; refresh from YouTube/Rock. |
| **History** | `/history` | Past sessions and per-platform outcomes (success/URL/error). |
| **Settings** | `/settings` | Run/clear each service login, authorize YouTube, check the LLM (title) backend status, manage encrypted secrets, change the shared password. |

**Typical run:** open `/` → pick folders + spreadsheet → map columns →
**Match dates** → select dates + platforms → **Upload** and watch progress.
The upload runs in batches of 4 dates; already-succeeded dates are skipped on a
re-run, so an interrupted run is safe to resume by re-selecting the rest.

---

## Configuration files

| File | Purpose | In git? |
|------|---------|---------|
| `config.yaml` | Per-platform schedules + on/off toggles, default element toggles, LLM settings, `upload.max_workers` | yes |
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
  tags / schedule, plus Rock children) are active.
- `description_footers` — text appended to descriptions per platform.
- `upload.max_workers` — parallel upload thread-pool size (default 4).
- `upload.circuit_breaker` — `failure_threshold` / `recovery_timeout_seconds`
  for the per-platform breaker that fast-fails a repeatedly-broken platform for
  the rest of a run (see the RUNBOOK "Health / triage" notes).
- `upload.youtube_wait_timeout_seconds` — how long the Rock Email row waits for
  its YouTube Video sibling's watch URL (default 1800).
- `llm` — besides `model` / `num_title_suggestions`, the title call's
  `temperature`, `max_tokens`, `request_timeout_seconds`, `health_timeout_seconds`,
  and a `circuit_breaker` block are all tunable here.

Media folders, the spreadsheet, and the column mapping are **not** in
`config.yaml` anymore — they're picked per browser session on the dashboard.

### Env var reference (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `FLASK_SECRET_KEY` | random per process | Signs the Flask session cookie. **Set this** or sessions reset on every restart. |
| `DLD_UPLOAD_TMP` | `/data/uploads` if `/data` exists, else `./.uploads` | Where transiently-uploaded media is reassembled per run. On the VPS this is on the `dld-data` volume. |
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
| `LLM_BASE_URL` | `http://localhost:8081` | OpenAI-compatible endpoint for Shorts title suggestions. Default is the bundled llamafile. For Ollama: `http://localhost:11434`. |
| `LLM_MODEL` | `local` | Model name sent to that endpoint. llamafile ignores it (`local`); Ollama needs a real name, e.g. `llama3.2`. |

---

## Hosting on a VPS (Linux)

The bundled `bin/` runtimes and `launch_mac.command` are **Mac-only and not
needed on a VPS** — you don't ship `bin/python_arm|intel`, `bin/node_arm|intel`,
or `bin/llamafile`. Install the equivalents from the OS instead. The simplest
path is Docker Compose (`deploy/docker-compose.yml`), which builds the app,
Ollama, Caddy, and a Cloudflare Tunnel; `cd deploy && docker compose up -d --build`.

**1. App + dependencies**
```bash
sudo apt install -y python3 python3-venv
git clone <this repo> && cd DailyLifeDistributor
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

**2. Required environment** (e.g. in `.env`)
```bash
SECRET_ENC_KEY=...            # generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FLASK_SECRET_KEY=...          # set a STABLE value (any long random string) so sessions survive restarts
INITIAL_ADMIN_PASSWORD=...    # seeds the login on first boot; change it in Settings afterward
ALLOWED_HOSTS=uploader.example.com   # your VPS hostname(s)
SESSION_COOKIE_SECURE=true    # you are behind HTTPS (see step 4)
```

**3. LLM for title suggestions (optional, via Ollama)**
```bash
ollama serve &           # or run as a service
ollama pull llama3.2
# then set:
LLM_BASE_URL=http://localhost:11434
LLM_MODEL=llama3.2
```
If you skip this, the app runs fine — titles just won't be auto-suggested.
Title suggestions read the spreadsheet's mapped transcript column (no audio
transcription).

**4. TLS + reverse proxy**
Put the app behind nginx or Caddy terminating HTTPS, proxying to the Flask port.
Keep `SESSION_COOKIE_SECURE=true` and set `ALLOWED_HOSTS` to your domain. (The
app uses Flask's built-in server; that's adequate for a single operator behind a
proxy — moving to a production WSGI server is a later hardening step.)

**5. Secrets**
On first boot the app auto-imports any plaintext secrets present, but on a fresh
VPS you'll typically have none — so enter your API keys (and authorize YouTube)
from the **Settings** page; everything is encrypted at rest with `SECRET_ENC_KEY`.

> 🖥️ **Browser-uploader auth on a headless VPS.** YouTube uses the API and works
> fully headless. The API-less platforms (SimpleCast, Vista Social, Rock)
> authenticate through an **interactive** browser login, which a bare headless
> server can't perform on its own — so the VPS deploy ships an in-container
> Chromium + a noVNC viewer wired up to a dedicated `/remote-login/*` flow
> (see `blueprints/remote_login.py`, `core/remote_login_playwright.py`, and
> `core/vnc.py`). From **Settings → API Credentials → Connect** the dashboard
> launches the headed login in-container; the operator drives it through the
> in-page noVNC iframe; on submit the resulting Playwright `storage_state` is
> encrypted with `SECRET_ENC_KEY` and stored in the `secrets` table next to
> the rest of the platform creds. The Phase-3 hybrid upload agent
> (`HYBRID_AGENT_ENABLED`) can additionally run the uploads locally on the
> operator's laptop against locally-stored sessions — that path bypasses the
> VPS-side login entirely when an agent is paired and online. See `docs/RUNBOOK.md`.

---

## Health & on-call

- **`/health`** — JSON probe for `state.db` writability, the LLM endpoint
  (`LLM_BASE_URL`, default llamafile on 8081), and the Chrome path. Returns HTTP
  503 if anything is down. Curl this first when something is wrong.
- **`logs/daily_life.log`** — rotating log file (5 MB × 5 backups).

### Recovery runbook

| Symptom | What's wrong | Fix |
|---|---|---|
| App fails to launch, port-conflict message on stderr | Stale Flask or another local server holding 8080/8081 | Kill the process or set `FLASK_PORT` |
| App refuses to start: "SECRET_ENC_KEY not set" | Missing encryption key | Generate via `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and set in `.env` |
| Login page loops forever, or you're logged out after every request | `SESSION_COOKIE_SECURE` is `true` but you're on plain http | Set `SESSION_COOKIE_SECURE=false` in `.env` for local development |
| Title suggestions never appear | LLM endpoint unreachable (llamafile not started, or Ollama not running / wrong `LLM_BASE_URL`/`LLM_MODEL`) | Check `/health`; confirm the LLM server is up at `LLM_BASE_URL` and the model name matches `LLM_MODEL` |
| `Failed to initialize state.db ...` on startup | `state.db` corrupt (USB unplug mid-write, `kill -9` mid-commit) | Back up the file, delete it, restart — a fresh schema is created |
| SimpleCast/Vista/Rock: "still on a login page after login" | Saved session is broken | Delete the matching `*_session.json` and retry — first-run login fires again |
| Headless run hangs and eventually errors | Saved Playwright session expired; uploader relaunched headed but no human typed | Re-run interactively and complete the login prompt |
| YouTube upload says "needs re-auth" | Refresh token revoked | Delete `token.json` and retry; consent flow opens in browser |
| Title suggestions say "no transcript" | The transcript column isn't mapped for that date | Map a transcript column on the dashboard before requesting suggestions |
| Batch upload rejected "not enough free disk space" | The data volume is low on space for the declared batch | Free space on the VPS data volume, or upload fewer dates per run |

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

- **Access control: shared-password login.** Every route except `/login`, `/health`, and static assets requires an authenticated session (signed cookie, `HttpOnly` / `SameSite=Lax` / `Secure`). The first password is seeded from `INITIAL_ADMIN_PASSWORD` and changeable in Settings; failed logins are rate-limited per IP. (The old loopback-only `before_request` guard has been removed so the app can be hosted on a VPS.)
- **Host validation:** when `ALLOWED_HOSTS` is set, the `Host` header must match one of the configured hostnames (DNS-rebind defense). Unset means no host restriction (local dev).
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
