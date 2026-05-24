# Operations runbook

Short, practical notes for running the hosted deploy (autoalert.pro).

## Backups — do this

Everything irreplaceable lives in **two** places:

1. **`SECRET_ENC_KEY`** (in `deploy/.env`) — the master key. Every secret in the
   app (YouTube refresh token, the SimpleCast/Vista/Rock browser sessions, the
   login password hash) is Fernet-encrypted with it. **If this key is lost, none
   of those secrets can ever be decrypted** — you'd re-authenticate every
   platform from scratch. Keep a copy in a password manager, off the box.

2. **`state.db`** (on the `dld-data` Docker volume, at `/data/state.db`) — holds
   the encrypted `secrets` table plus `upload_history`, `sessions`, and
   `image_history`. Back it up periodically.

Quick manual backup from the VPS:

```bash
# dump the SQLite file out of the running container
docker exec dld sh -c 'cat /data/state.db' > state.db.$(date +%F).bak
# and make sure SECRET_ENC_KEY is saved somewhere safe (NOT next to this file)
grep '^SECRET_ENC_KEY=' ~/DailyLifeDistributor/deploy/.env
```

Restore = put `state.db` back on the volume and ensure the **same**
`SECRET_ENC_KEY` is in `.env`. A different key makes the secrets undecryptable.

## If an upload is interrupted

The pipeline holds the picked files in the **browser** and drives the upload
itself, so a server restart, container redeploy, or closed tab stops the run.
Recovery is built in and safe:

- Re-open the dashboard, re-pick the same folders + spreadsheet, **Match dates**,
  and select the dates that didn't finish.
- Already-succeeded `(date, platform)` rows are **skipped automatically**
  (idempotent re-run), so you won't get duplicate YouTube videos / SimpleCast
  drafts.
- Temp files from the dead run are deleted on the next startup/idle **orphan
  sweep**, so disk doesn't leak.

Keep the tab open for the duration of a run — that's why the dashboard warns you.

## Connecting YouTube (hosted instance)

YouTube uses Google OAuth, which on the headless server needs the **web
redirect** flow (the desktop "open a browser on this machine" flow can't work
on the VPS). One-time Google Cloud setup:

1. Google Cloud Console → **APIs & Services → Credentials → Create credentials
   → OAuth client ID**, application type **Web application**.
2. Under **Authorized redirect URIs** add exactly:
   `https://autoalert.pro/oauth/youtube/callback`
3. Download the JSON and upload it under **Settings → YouTube Client Secrets**
   (it replaces the old Desktop client_secrets).
4. Click **Connect YouTube** → consent in your own browser → you're redirected
   back and the token is stored (encrypted) in `state.db`.

If the client is still a Desktop ("installed") client, Connect YouTube returns a
clear error telling you to create the Web client. The SimpleCast/Vista/Rock
"Connect" buttons are unrelated — those use the streamed browser, not Google
OAuth.

## Deploy

```bash
wsl ssh dropshippa
cd ~/DailyLifeDistributor && git pull && cd deploy && docker compose up -d --build
```

## Health / triage

- `GET /health` (needs the `autoalert.pro` Host header) reports the SQLite file,
  the title LLM (Ollama) reachability, and Chrome availability.
- `docker ps` shows the app container's health (`healthy` / `unhealthy`) — the
  compose `healthcheck` polls `/health` every 60s. **Unhealthy does not restart
  the app** (compose has no `on-failure` for healthchecks); it's a signal to
  curl `/health` for the failing subsystem. An LLM (Ollama) blip alone will flip
  it unhealthy even though uploads/login still work — check the JSON.
- App logs: `docker logs dld --tail 50` (also persisted to `logs/daily_life.log`,
  rotating at 5 MB × 5 backups).
- A platform login that silently expired surfaces as a per-row upload error;
  fix it under **Settings → Connect** (re-auth via the streamed browser).
- If a platform fails repeatedly inside one run (e.g. a broken session), its
  **circuit breaker opens** and the remaining dates for that platform are
  skipped fast (per-row "temporarily disabled" error) instead of relaunching
  Chrome each time. Re-Connect the platform in Settings, then re-run — completed
  rows are skipped idempotently and the breaker re-probes after a short cooldown.

## Known limitations

- In-memory run state (single Flask worker): one upload run at a time; a restart
  mid-run drops the in-flight run (see recovery above).
- Chunk size is 95 MB to stay under Cloudflare's ~100 MB proxied-body cap; drop
  `DLD_MAX_RUN_BYTES` / chunk size if very large files bounce through the tunnel.

## Agent fleet appears offline

Symptoms: dashboard "Agent" chip is grey, `?path=agent` runs fail with
`NoAgentOnlineError`, or `/health/details` reports `agents_online: 0`.

```bash
curl https://autoalert.pro/health/details | jq .agents_online   # expect >= 1
docker logs dld 2>&1 | grep "relay:" | tail -20                  # look for "agent registered" / "unregistered"
```

Common causes:

- **Cloudflare Tunnel restarted** — every agent has to reconnect. Check
  `docker logs dld-cloudflared --tail 50` for "connection lost" / "reconnected".
- **Agent's saved token revoked** — Owner unpaired the device from
  `/settings/devices`. The agent will log a 401 in its own log and exit;
  re-pair from the agent GUI.
- **Server restarted within the agent's reconnect window** — the agent
  uses exponential backoff with jitter (3-60s). Wait up to a minute and
  agents come back on their own.

Quick triage of a single agent: open the agent GUI on the host machine,
or `tail` its log (`%LOCALAPPDATA%\DailyLifeAgent\agent.log` on Windows,
`~/Library/Logs/DailyLifeAgent/agent.log` on macOS).

## Resend deliverability incident

Symptoms: users report missing invites / 2FA codes / new-device alerts.

```bash
curl https://autoalert.pro/health/details | jq .resend_configured           # expect true
curl https://autoalert.pro/health/details | jq '.breakers["email:resend"]'  # state should be "closed"
```

- `resend_configured: false` → `RESEND_API_KEY` is empty in `.env`. Re-add it,
  restart the container, retry.
- `breakers["email:resend"].state == "open"` → recent failures tripped the
  per-process breaker. It cools down in 60s and re-probes; if it stays open
  the upstream is degraded (check the Resend status page).
- DNS / domain verification check:

  ```bash
  dig TXT _dmarc.autoalert.pro
  dig CNAME <selector>._domainkey.autoalert.pro
  ```

  If either record is missing the Resend dashboard will show the domain as
  unverified — fix DNS, click "verify" in the dashboard, then retry.

Recovery emails sent during an outage are NOT silently dropped — the route
records the request in `recovery_requests` and emails when Resend recovers.
A user who didn't get their email can re-request via `/recover` (rate-limit:
1 per 24h per user). Owner can manually approve from `/admin/recovery-requests`.

## SECRET_ENC_KEY rotation

**Read this before you run anything.** `SECRET_ENC_KEY` is the master Fernet
key that encrypts every secret in the DB (YouTube refresh token, Playwright
session blobs, TOTP secrets, password reset tokens). **Lose it = lose every
encrypted blob. Forever.** There is no recovery path; every platform has to
be re-authenticated from scratch.

Pre-rotation checklist:

1. Back up `state.db`: `docker exec dld sh -c 'cat /data/state.db' > state.db.pre-rotate.bak`
2. Save the **old** key somewhere safe (a copy in your password manager,
   not on the box). You'll need it for the rotation itself, and as a
   panic-button if rotation goes sideways.
3. Generate the new key:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

Run:

```bash
docker exec -e OLD_SECRET_ENC_KEY=<old> dld \
    python scripts/rotate_secret_enc_key.py --from-env OLD_SECRET_ENC_KEY \
        --new <new> --apply
```

This decrypts every row in the `secrets` table + every
`users.totp_secret_encrypted` with the old key, re-encrypts with the new,
and writes back in a single transaction. Dry-run (no `--apply`) prints
what would change without writing.

Post-rotation:

1. Update `deploy/.env` with the new `SECRET_ENC_KEY`.
2. Restart the container: `docker compose up -d`.
3. Verify: `curl https://autoalert.pro/health/details | jq .secret_enc_key_set`
   should be `true`, and `/health` should be `200`.
4. Test one encrypted read: open any "Connect" page that lists a saved
   browser session; if it loads, decryption works with the new key.

If the new key is wrong, the app fails closed (`MasterKeyError`) on the
first decrypt — revert `.env` to the old key, restart, and investigate.

## Ops scripts

Pure-Python, runnable inside the container as
`docker exec dld python scripts/<name>.py --help`. All honor
`DLD_STATE_DB` for the per-test / per-deploy DB path.

| Script | Use |
|--------|-----|
| `scripts/rotate_secret_enc_key.py` | One-shot rotation of `SECRET_ENC_KEY` (see above) |
| `scripts/breaker_status.py` | Dump every circuit breaker (`name | state | failures | seconds since open`). Triage a slow upload / image gather |
| `scripts/list_agents.py` | Paired devices table — id, name, hostname, hwid prefix, last seen, revoked / online |
| `scripts/quota_status.py` | Today's YouTube quota: global + per-org rows, % remaining |
| `scripts/show_program_owner.py` | Print the program-owner user(s) and every org they're a member of |
