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

## Deploy

```bash
wsl ssh dropshippa
cd ~/DailyLifeDistributor && git pull && cd deploy && docker compose up -d --build
```

## Health / triage

- `GET /health` (needs the `autoalert.pro` Host header) reports the SQLite file,
  the title LLM (Ollama) reachability, and Chrome availability.
- App logs: `docker logs dld --tail 50`.
- A platform login that silently expired surfaces as a per-row upload error;
  fix it under **Settings → Connect** (re-auth via the streamed browser).

## Known limitations

- In-memory run state (single Flask worker): one upload run at a time; a restart
  mid-run drops the in-flight run (see recovery above).
- Chunk size is 95 MB to stay under Cloudflare's ~100 MB proxied-body cap; drop
  `DLD_MAX_RUN_BYTES` / chunk size if very large files bounce through the tunnel.
