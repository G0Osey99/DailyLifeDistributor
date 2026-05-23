# Multi-Tenant Migration & Setup

How to roll out the multi-tenant rewrite (PRs α/β/γ/δ — merged 2026-05-23) onto the existing hosted instance and any new deployment.

## What changes for end users

- Login is **username + password** (not just a shared password). On first boot the migration creates `admin` / `<PROGRAM_OWNER_EMAIL>` with the existing `INITIAL_ADMIN_PASSWORD` as the seed. First login forces a password change.
- A switch-org dropdown appears in the header when a user is in more than one org.
- The legacy shared-password form stays available **only** while `LEGACY_PASSWORD_ENABLED=true` is set in `deploy.env`. Leave it on for the first deploy, then flip off once you've verified the new login works.
- `/admin` becomes available to the bootstrap user (program-owner) for cross-org management.
- `/settings/members`, `/settings/2fa`, `/settings/security`, `/settings/audit-log`, `/download/agent`, `/recover` are new.
- All emails (invite, welcome, 2FA, recovery, new-device, password-reset) go through Resend. **Without `RESEND_API_KEY`, sends are no-ops** (warning logged) — the app still works, but invites/2FA-by-email/recovery do not function.

## Required env vars (deploy.env)

```bash
# Existing — keep these
SECRET_ENC_KEY=<existing-fernet-key>            # DO NOT regenerate; used for TOTP + secrets encryption
INITIAL_ADMIN_PASSWORD=<existing-shared-pw>     # seeds the bootstrap user's password
HOSTED=true
ALLOWED_HOSTS=autoalert.pro
DLD_STATE_DB=/data/state.db

# NEW — required on first boot of multi-tenant code
PROGRAM_OWNER_EMAIL=ryker@example.com           # the bootstrap user's email (used for recovery + as username's contact)

# NEW — recommended
LEGACY_PASSWORD_ENABLED=true                    # keep shared-password login alive for one release as safety net
RESEND_API_KEY=re_<...>                         # without this, ALL emails are no-ops
RESEND_FROM=noreply@autoalert.pro               # outbound From address

# NEW — optional
DLD_DISK_MIN_FREE_BYTES=5368709120              # web upload admission threshold (default 5 GiB)
```

If `PROGRAM_OWNER_EMAIL` is missing on first boot, the migration aborts with a clear message. Once a program-owner user exists in the DB, the env var can be unset on subsequent boots.

## First-boot migration (automatic)

`app.create_app()` calls `core.migration_bootstrap.run_migration()` after `init_db()`. The migration is idempotent and does the following:

1. **Schema** — adds `organizations`, `users`, `org_memberships`, `invitations`, `recovery_codes`, `recovery_requests`, `audit_log`, `audit_log_archive`, `email_2fa_codes`, `login_ip_sightings`, `platform_locks`, `yt_quota_usage`. ALTERs `agent_devices.user_id`, `secrets.org_id`, `upload_history.{org_id, user_id}`, `users.totp_enabled`, `users.notify_new_device`, `recovery_requests.note`. All nullable + idempotent.
2. **LCBC Church org** — creates `name="LCBC Church", slug="lcbc-church"` if missing.
3. **Bootstrap user** — creates `username="admin", email=$PROGRAM_OWNER_EMAIL` with `password_hash` derived from `$INITIAL_ADMIN_PASSWORD`, `program_owner=TRUE`. `password_changed_at=NULL` so the first login is forced to change the password.
4. **Owner membership** — bootstrap user becomes Owner of LCBC Church.
5. **Backfills**:
   - `agent_devices.user_id` = bootstrap user's id for all existing rows
   - `secrets.org_id` = LCBC Church id for all existing rows
   - `upload_history.{org_id, user_id}` = LCBC Church id, bootstrap user id for all existing rows

Re-running the migration (e.g. container restart) is a no-op.

## Resend (DKIM/SPF/DMARC)

For invites and 2FA emails to reach the inbox, add DNS records on `autoalert.pro` (Cloudflare):

```
# DKIM — values come from the Resend dashboard after adding the domain
<selector>._domainkey  CNAME   <resend-provided-target>

# SPF
@                      TXT     v=spf1 include:_spf.resend.com -all

# DMARC
_dmarc                 TXT     v=DMARC1; p=quarantine; rua=mailto:postmaster@autoalert.pro
```

Resend dashboard → Domains → `autoalert.pro` → wait for verification. Then set the Resend API key on the VPS.

## Deploy to the VPS

```bash
wsl ssh dropshippa
cd ~/DailyLifeDistributor
git pull
# edit deploy.env: add PROGRAM_OWNER_EMAIL, LEGACY_PASSWORD_ENABLED, RESEND_API_KEY, RESEND_FROM
cd deploy
docker compose up -d --build
docker logs -f dld | head -50   # watch for "Migration: created 'LCBC Church'" and "Migration: created bootstrap program-owner admin"
```

## Post-deploy verification

1. Visit https://autoalert.pro/login — confirm both the new username+password form and the legacy single-password form render.
2. Log in with the **legacy** form (one last time) — verify the existing dashboard works.
3. Log out, log in with **`admin` + the old INITIAL_ADMIN_PASSWORD** — get redirected to "change your password" — set a new one.
4. After re-login, you should see `/admin` in the nav. Visit it; the LCBC Church org should be listed with the bootstrap user as Owner.
5. Hit `/settings/members` — confirm the page loads.
6. Hit `/settings/2fa` — enroll TOTP, save the recovery codes, log out, log back in, enter a TOTP code.
7. Hit `/download/agent` from a Windows browser — Windows button should be highlighted; pairing code should be visible.
8. Once everything's confirmed: in `deploy.env`, set `LEGACY_PASSWORD_ENABLED=false`, `docker compose up -d`, log out, confirm the legacy form is gone.

## Rollback

If the new login is broken, set `LEGACY_PASSWORD_ENABLED=true` in `deploy.env` and `docker compose up -d`. The schema changes are backward-compatible (all new columns nullable, all new tables unused by old code paths). Existing data is intact.

If even legacy login fails, you can downgrade the image:

```bash
cd ~/DailyLifeDistributor
git checkout <previous-main-sha>
cd deploy
docker compose up -d --build
```

The schema additions stay (they are nullable so the old code ignores them).

## Known follow-ups (not blockers)

These are tracked in the merged PR descriptions and don't block production use:

- Wire `core/platform_locks.try_acquire()` into `core/upload_jobs._dispatch_upload` and `core/agent_dispatch.start` (soft-lock against concurrent same-platform uploads within an org). Helper + table are ready; integration deferred.
- Per-org room keying for `core/relay.py` (currently `_ACCOUNT="default"` — fine while only LCBC Church exists; required before second org goes live).
- `agent_dispatch.collect_credentials(org_id)` plumbing — agent currently uses the default org's secrets.
- Audit hooks for `core/upload_jobs.py`, `blueprints/devices.py`, `blueprints/secrets.py` (writer + schema ready; missing event emits).
- `@require_role` decorator rollout to existing state-changing routes — decorators currently no-op while `LEGACY_PASSWORD_ENABLED=true`.
- `download.requested`, `platform_lock.contention`, `quota.exceeded` audit event types.
