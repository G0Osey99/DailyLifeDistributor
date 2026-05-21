# DailyLifeDistributor

Local Flask app that publishes a day's worth of media — full-length YouTube
videos, YouTube Shorts, and a SimpleCast podcast episode — from a folder of
files plus an Excel planning sheet, in one workflow. Optional integrations
push the same media to Vista Social and Rock RMS.

The whole thing runs on the operator's Mac (or any Python 3.11+ machine),
binds only to loopback, and stores its state on the USB drive next to the
media. There is no server-side component.

## Quick start

```bash
./launch_mac.command          # Mac, primary launch path
# or
pip install -r requirements.txt
python app.py                 # any platform; opens http://localhost:8080
```

The launch script picks the right bundled Python (`bin/python_arm/` or
`bin/python_intel/`), starts `bin/llamafile` for title suggestions on port
8081, then starts Flask on 8080.

## First-run setup

You need three things before the first upload will succeed:

1. **YouTube OAuth credentials.** In Google Cloud Console, create an OAuth
   client (Desktop app), download the JSON, and save it as
   `client_secrets.json` at the project root. The first YouTube upload pops
   a browser to consent; the resulting refresh token lands in `token.json`.

2. **SimpleCast login.** The first SimpleCast upload opens a Chrome window
   on the SimpleCast login page and waits up to `SIMPLECAST_LOGIN_TIMEOUT`
   seconds (default 300) for you to log in manually. On success the cookies
   are saved to `simplecast_session.json` and every subsequent run reuses
   them. Same pattern applies to Vista Social and Rock if those are enabled.

3. **Google Chrome.** Playwright drives the system Chrome via
   `channel='chrome'` — `playwright install` is **not** needed. If Chrome
   is in a non-standard location, set `SIMPLECAST_CHROME_PATH` (or the
   per-service equivalent) to the binary path.

## Configuration

| File | Purpose |
|------|---------|
| `config.yaml` | Paths, schedules per platform, platform on/off toggles, `upload.max_workers`, LLM settings |
| `.env` | Secrets and Playwright env knobs (see Env reference below) |
| `client_secrets.json` | Google OAuth client (you provide) |
| `token.json` | YouTube refresh token (auto-generated) |
| `*_session.json` | Saved Playwright sessions (auto-generated) |
| `state.db` | SQLite — sessions, upload_history, image_history |

### Env var reference

| Variable | Default | Purpose |
|---|---|---|
| `FLASK_SECRET_KEY` | random per process | Signs the Flask session cookie. **Set this** or sessions die on every restart. |
| `FLASK_PORT` | `8080` | Override the bind port. |
| `FLASK_DEBUG` | `false` | Enable Flask's debug reloader. |
| `YOUTUBE_CLIENT_SECRETS_PATH` | `./client_secrets.json` | Path override for the OAuth client JSON. |
| `SIMPLECAST_UPLOAD_URL` | show-scoped default | Override the SimpleCast new-episode URL. |
| `SIMPLECAST_HEADLESS` | `false` | `true` to hide Chrome on subsequent runs (first login is always headed). |
| `SIMPLECAST_LOGIN_TIMEOUT` | `300` | Seconds to wait for first-run manual login. |
| `SIMPLECAST_CHROME_PATH` | (auto) | Full path to Chrome if `channel='chrome'` can't find it. |
| `VISTA_SOCIAL_*` / `ROCK_*` | parallel | Same pattern for the other Playwright uploaders. |

## Operator workflow

1. **Index** (`/`) — pick dates and platforms.
2. **Review** (`/review`) — edit titles, descriptions, schedules; LLM
   suggests Shorts titles.
3. **Confirm** (`/confirm`) — preview before kicking off uploads.
4. **Upload** (`/upload`) — runs platform uploads in parallel
   (`config.upload.max_workers`, default 4); progress streams via SSE.
5. **History** (`/history`) — past sessions and per-row outcomes from
   `upload_history`.

## Health & on-call

- **`/health`** — JSON probe for `state.db` writability, llamafile (port
  8081), and Chrome path. Returns 503 if anything is down. Curl this first
  when something is wrong.
- **`logs/daily_life.log`** — rotating file (5 MB × 5 backups).

### Recovery runbook

| Symptom | What's wrong | Fix |
|---|---|---|
| App fails to launch, port-conflict message on stderr | Stale Flask or another local server holding 8080/8081 | Kill the process or set `FLASK_PORT` |
| Title suggestions never appear | llamafile crashed or never started | Check `/health`, re-run launch script, or start `bin/llamafile` manually |
| `Failed to initialize state.db ...` on startup | `state.db` corrupt (USB unplug mid-write, kill -9 mid-commit) | Back up the file, delete it, restart — a fresh schema is created |
| SimpleCast: "still on a login page after login" | Saved session is broken | Delete `simplecast_session.json` and retry — first-run login will fire |
| Headless run hangs and eventually errors | Saved Playwright session expired; uploader auto-relaunched headed but no human typed | Re-run interactively and complete the login prompt |
| YouTube upload says "needs re-auth" | Refresh token revoked | Delete `token.json` and retry; consent flow opens in browser |

### `state.db` schema

Three tables managed by `core/db.py`:

- `sessions` — workflow state JSON + label + status. One row per workflow
  run; lets you resume an in-progress session after a crash.
- `upload_history` — one row per platform attempt: `success`, `url`,
  `scheduled_time`, `error`. Powers the History page.
- `image_history` — Unsplash image-gather attribution log used by the Rock
  Vistas integration.

The full schema lives in `core/db.py:init_db()`. To inspect:

```bash
sqlite3 state.db ".schema"
```

### YouTube quota

`QUOTA_COSTS` in `app.py` tracks estimated API units per operation
(video upload=100, thumbnail=50, etc.) against a `DAILY_QUOTA` of 10,000.
Counters are stored in the Flask session and reset on the calendar day in
`scheduling.timezone`.

## Architecture

Single Flask app (`app.py`) backed by `core/`, `blueprints/`, and uploader
modules under `uploaders/`. See `CLAUDE.md` for the full architecture
breakdown — that file is the engineering reference, this README is the
operator reference.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Integration tests in `tests/integration/` hit live network sources and are
skipped by default; opt in with the env vars documented in their
docstrings.

## Security model

- Flask binds to `0.0.0.0` from the launch script for LAN convenience but
  `app.py` rejects every non-loopback request in `before_request` (HTTP
  403). Functionally local-only.
- DNS-rebinding defense: the `Host` header must name `localhost` or
  `127.0.0.1`.
- CSRF: state-changing requests must be same-origin (`Sec-Fetch-Site`)
  or carry an `Origin`/`Referer` matching the host.
- The `*_session.json` files are gitignored but contain dashboard cookies.
  If you back up the USB to cloud storage, those credentials travel — keep
  the backup destination scoped accordingly.
