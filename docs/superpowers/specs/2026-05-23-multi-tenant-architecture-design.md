# Multi-Tenant Architecture — Design

**Date:** 2026-05-23
**Status:** Approved (brainstorm) — pending implementation plan
**Scope:** Convert the single-tenant shared-password app into a multi-tenant SaaS with organizations, role-based access, invite-based account creation, email delivery via Resend, 2FA (TOTP + email), audit logging, and concurrency lifts for the agent path. Existing data migrates into a single "LCBC Church" org owned by the program-owner.

## Problem

The app today is single-tenant: one global INITIAL_ADMIN_PASSWORD gates all access. Credentials, devices, history, and the relay are all keyed to a singleton `_ACCOUNT="default"`. The product needs multi-user support for the program-owner to onboard external organizations without sharing credentials, and to scale beyond one customer.

## Goal

A multi-tenant model where:
- The program-owner (one specific user) creates organizations via an admin UI and invites their Founding Owner.
- Each organization owns its own credentials (YouTube, Rock, SimpleCast, Vista), audit log, upload history, and pool of agent devices (devices themselves are owned by individual users within the org).
- Users authenticate via username + password (Argon2id), optionally with TOTP or email 2FA, with admin-approved recovery for catastrophic auth loss.
- Invites are the only path to account creation; sent via Resend transactional email; signed time-limited tokens.
- Web-only uploads keep their concurrency lock but per-user (was global); agent-path uploads have no lock (each user's agent is on a different machine).
- Existing data migrates into a single LCBC Church org owned by the program-owner.

## Decisions (from brainstorm)

| Question | Decision |
|----------|----------|
| Owner count per org | Multiple Owners allowed. Founding Owner can promote others to Owner. |
| Email provider | Resend.com. Free tier (3000/mo) covers expected volume. DKIM/SPF/DMARC required on autoalert.pro. |
| Lost password + lost 2FA + lost recovery codes | Admin-approved out-of-band recovery: user submits request → email goes to all Owners of their org → an Owner approves → user gets a new password-set email → 2FA is reset on next login. |
| Existing data migration | Roll forward into a "LCBC Church" org owned by the program-owner. Continuity preserved; no manual re-pairing or re-credential-setup. |
| Agent download UX | Empty-state card on the dashboard (first-time onboarding) AND a persistent entry in `/settings/devices` (for re-installs, adding a second machine). |
| Pricing/billing hooks | Leave structural hooks (org `plan` field defaults to `'free'`, `billing_email` field reserved). No Stripe wiring yet. |
| 2FA enforcement | Org-level toggle. Owner can flip "Require 2FA"; once on, members must enable on next login or they're locked out of upload features (account stays alive for setup completion). |
| Audit retention | 365 days online, then archived to a separate `audit_log_archive` table (still queryable by the program-owner). |

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Browser                                                                  │
│ - Login form: username + password (+ TOTP / email code if 2FA on)       │
│ - Switch-org dropdown when user is in multiple orgs                     │
│ - Per-org dashboard, settings, devices, upload history                  │
└─────────────────────────────────────────────────────────────────────────┘
                            │ session cookie (user_id + org_id)
                            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Flask app                                                                │
│ - auth: argon2id + TOTP + email 2FA + recovery codes                    │
│ - authz: role checks on every route (Owner / Manager / User)            │
│ - invites: signed tokens via Resend email                               │
│ - admin: program-owner-only org management                              │
│ - audit log: 365d online → archive table                                │
└─────────────────────────────────────────────────────────────────────────┘
        │                                       │
        ▼                                       ▼
┌────────────────────┐                ┌────────────────────┐
│ SQLite (state.db)  │                │ Resend API         │
│ - users            │                │ (transactional     │
│ - organizations    │                │  email: invites,   │
│ - org_memberships  │                │  password reset,   │
│ - invitations      │                │  2FA codes,        │
│ - recovery_codes   │                │  recovery,         │
│ - recovery_requests│                │  notifications)    │
│ - audit_log        │                │                    │
│ - audit_log_archive│                │                    │
│ - secrets (org_id) │                │                    │
│ - agent_devices    │                └────────────────────┘
│   (user_id)        │
│ - upload_history   │
│   (org_id+user_id) │
└────────────────────┘
```

## Data model

### New tables

**`organizations`**
- `id INTEGER PK`
- `name TEXT NOT NULL` (e.g. "LCBC Church")
- `slug TEXT UNIQUE NOT NULL` (URL-safe; e.g. "lcbc-church")
- `plan TEXT NOT NULL DEFAULT 'free'` (billing-hook placeholder)
- `billing_email TEXT NULL` (billing-hook placeholder)
- `require_2fa BOOLEAN NOT NULL DEFAULT FALSE`
- `created_at TIMESTAMP`
- `created_by_user_id INTEGER FK users(id) NULL` (NULL for the migrated LCBC org since the user is created in the same migration)
- `disabled_at TIMESTAMP NULL` (program-owner can disable an org)

**`users`**
- `id INTEGER PK`
- `username TEXT UNIQUE NOT NULL`
- `email TEXT UNIQUE NOT NULL`
- `password_hash TEXT NOT NULL` (Argon2id)
- `totp_secret_encrypted TEXT NULL` (Fernet-encrypted using existing `SECRET_ENC_KEY`)
- `email_2fa_enabled BOOLEAN NOT NULL DEFAULT FALSE`
- `program_owner BOOLEAN NOT NULL DEFAULT FALSE` (the special flag; exactly one user has it)
- `created_at TIMESTAMP`
- `last_login_at TIMESTAMP NULL`
- `password_changed_at TIMESTAMP NULL` (force change on first login after migration)

**`org_memberships`**
- `id INTEGER PK`
- `user_id INTEGER FK users(id) NOT NULL`
- `org_id INTEGER FK organizations(id) NOT NULL`
- `role TEXT NOT NULL CHECK(role IN ('owner','manager','user'))`
- `joined_at TIMESTAMP`
- `UNIQUE(user_id, org_id)`

**`invitations`**
- `id INTEGER PK`
- `org_id INTEGER FK organizations(id) NOT NULL`
- `inviter_user_id INTEGER FK users(id) NOT NULL`
- `email TEXT NOT NULL`
- `role TEXT NOT NULL CHECK(role IN ('owner','manager','user'))`
- `token_hash TEXT NOT NULL` (sha256 of the raw signed token)
- `expires_at TIMESTAMP NOT NULL` (7 days from creation)
- `accepted_at TIMESTAMP NULL`
- `revoked_at TIMESTAMP NULL`
- `created_at TIMESTAMP`

**`recovery_codes`**
- `id INTEGER PK`
- `user_id INTEGER FK users(id) NOT NULL`
- `code_hash TEXT NOT NULL` (bcrypt-hashed; sha256 would be too cheap to brute)
- `used_at TIMESTAMP NULL`
- `created_at TIMESTAMP`

**`recovery_requests`**
- `id INTEGER PK`
- `user_id INTEGER FK users(id) NOT NULL`
- `requested_at TIMESTAMP NOT NULL`
- `expires_at TIMESTAMP NOT NULL` (48 hours from creation)
- `approver_user_id INTEGER FK users(id) NULL`
- `approved_at TIMESTAMP NULL`
- `password_reset_token_hash TEXT NULL` (issued on approval)
- `consumed_at TIMESTAMP NULL`

**`audit_log`** (active table; 365 days online)
- `id INTEGER PK`
- `org_id INTEGER FK organizations(id) NULL` (NULL for cross-org events like login)
- `actor_user_id INTEGER FK users(id) NULL` (NULL for system events like migration)
- `action TEXT NOT NULL`
- `target_type TEXT NULL` ('user', 'org', 'device', 'invite', 'upload', ...)
- `target_id INTEGER NULL`
- `metadata TEXT NULL` (JSON blob)
- `ip TEXT NULL`
- `user_agent TEXT NULL`
- `created_at TIMESTAMP NOT NULL`
- Index on `(org_id, created_at)` for fast org-scoped queries
- Index on `(actor_user_id, created_at)`

**`audit_log_archive`** (mirror schema; nightly job moves rows older than 365 days)

### Modified tables

**`agent_devices`** — adds:
- `user_id INTEGER FK users(id) NULL` (NULL = legacy; migration fills in)

**`secrets`** — adds:
- `org_id INTEGER FK organizations(id) NULL` (NULL = legacy / migrated to LCBC by migration)

**`upload_history`** — adds:
- `org_id INTEGER FK organizations(id) NULL` (migration fills)
- `user_id INTEGER FK users(id) NULL` (migration fills with program-owner)

**Flask session cookie** — gains `user_id`, `current_org_id`. The "authenticated" boolean stays during transition; eventually deprecated in favor of `user_id is not None`.

## Roles & permissions

| Permission | Owner | Manager | User | Program-Owner (via /admin) |
|---|:---:|:---:|:---:|:---:|
| Manage org settings (name, 2FA enforcement, plan) | ✓ |  |  | ✓ |
| Invite Owners | ✓ |  |  | ✓ |
| Invite Managers | ✓ |  |  | ✓ |
| Invite Users | ✓ | ✓ |  | ✓ |
| Revoke pending invites | ✓ (any) | ✓ (own + Users') |  | ✓ |
| Remove Owners | ✓ (with consent) |  |  | ✓ |
| Remove Managers | ✓ |  |  | ✓ |
| Remove Users | ✓ | ✓ |  | ✓ |
| Promote/demote roles | ✓ |  |  | ✓ |
| Connect platform credentials (YouTube, Rock, etc) | ✓ | ✓ |  | (on-behalf-of via admin) |
| Run uploads | ✓ | ✓ | ✓ |  |
| Pair / revoke own devices | ✓ | ✓ | ✓ |  |
| View audit log | ✓ | ✓ |  | ✓ (cross-org) |
| Create new organizations |  |  |  | ✓ |
| Disable an organization |  |  |  | ✓ |

Program-Owner does not appear in any specific org by default. They have a dedicated `/admin` UI and don't show up in member lists. They can "view as" an org for support purposes (with an audit log entry).

## Auth & 2FA

### Login
- Username + password. Argon2id hashing (parameters: `time_cost=2, memory_cost=65536, parallelism=4` — current OWASP defaults).
- Username is the login identifier; email is the contact identifier (for invites, recovery).
- Failed-login throttle: 5 attempts per 15 minutes per IP; lockout per-username after 10 consecutive failures (15-minute cool-down).

### TOTP
- `pyotp` library. 6-digit codes, 30-second window, drift tolerance ±1 step.
- Secret stored in `users.totp_secret_encrypted` (Fernet via existing `SECRET_ENC_KEY`).
- QR code generated via `qrcode[pil]` at setup. Shown once; user scans into their authenticator app.

### Email 2FA
- 6-digit code, valid 10 minutes, single-use.
- Sent via Resend on every login attempt where email 2FA is enabled.
- Less secure than TOTP (email account compromise = bypass) but more accessible for users without an authenticator app. Banner at setup: "Email 2FA is less secure than an authenticator app. If you can use an authenticator app, prefer that."

### Backup recovery codes
- 10 codes generated at 2FA enable.
- Each code is 8 chars, alphanumeric (collision-resistant entropy: ~47 bits — enough since codes are single-use).
- Stored bcrypt-hashed in `recovery_codes`.
- Shown once on a dedicated "Save these codes" page with a "Download as .txt" button.
- Each code single-use; using one decrements the available pool. At 2 codes remaining, banner says "Generate new backup codes" with one-click regeneration.

### Sessions
- Flask session, keyed by `user_id` and `current_org_id`.
- Session expiry: 30-day rolling (refreshed on activity).
- Switch-org dropdown in header when `len(user.memberships) > 1`.

### Password reset
- "Forgot password" link → email field → if account exists, Resend email with signed time-limited (1 hour) token URL → click → set new password → log in.
- If account doesn't exist, no indication is shown (no user-enumeration).

### Admin-approved out-of-band recovery
- User triggers "I lost my password AND 2FA AND my recovery codes" from the login screen.
- Form: enter username + a free-form note explaining the situation.
- Server creates a `recovery_requests` row, valid 48 hours.
- Email goes to all Owners of all orgs the user is a member of. Email contains: requester's username, the note, a link to `/admin-actions/recovery/<id>/approve`.
- Any Owner can approve. Approval emails the user a password-set link (1-hour token) AND clears their TOTP secret + invalidates recovery codes. User sets password, logs in, optionally sets up 2FA again.
- Audit log records the entire flow (request, approver, timestamps).
- Rate limit: 1 recovery request per user per 24 hours.

## Invite flow

1. Owner or Manager opens `/settings/members` → "Invite member" → email + role (Manager can only invite Users).
2. Server creates `invitations` row with a 32-byte signed token (`itsdangerous.URLSafeTimedSerializer`) hashed to DB. Plain token only in the outgoing email URL.
3. Resend sends an email from `noreply@autoalert.pro`:
   - Subject: `You've been invited to {org_name} on Daily Life Distributor`
   - Body: org name, inviter's name, role, "Accept invite" button → `https://autoalert.pro/invite/accept?token=...`
   - **Includes the agent download link** ("After accepting, download the agent for your machine: [Windows] [macOS]")
   - Expires in 7 days
4. Recipient clicks → server validates token → presents signup form:
   - Username (3-32 chars, alphanumeric + underscore + hyphen)
   - Password (Argon2id; min 12 chars; not in HaveIBeenPwned top 10K — check via local hash list, NOT API)
   - Optional 2FA setup wizard (TOTP recommended; email 2FA as fallback)
5. On submit: create user, create org_membership, mark invitation accepted, log audit event, send welcome email with the dashboard URL + agent download link, redirect to dashboard.
6. **Invite revocation**: Owner/Manager can revoke pending invites from `/settings/members`. Sets `revoked_at`; the token URL returns "This invitation has been revoked."
7. **Rate limits**: Managers can send 5 invites per hour; Owners are uncapped. Per-org spam check: same email can't have >3 pending invites.

## Org-scoped credentials + relay

### Secrets
- `secrets.org_id` is set on every new secret.
- `core/secrets_store.py` API extended: `get_secret(key, org_id)`, `set_secret(key, value, org_id)`, etc. Old single-arg calls become equivalent to `org_id=None` (legacy access — only used by migration code and the program-owner admin).

### Relay
- `core/relay.py` already account-keyed (single account "default"). Replace `_ACCOUNT = "default"` with per-org accounts: each org is a relay room.
- Agent's device record carries both `user_id` and (via the membership) the `org_id`. When the agent connects, its token resolves to a user_id which resolves to the org rooms it can join.
- A user is in one org room at a time per session (the currently-selected `current_org_id`). Switching orgs reconnects the wss.

### Dispatch
- `core/agent_dispatch.py`'s `_pick_device` already takes a device_id (PR #45 added this). The fallback chain now also restricts to **the current user's** devices, not the whole org's. (Devices are per-user; one user's agent shouldn't run another user's upload.)

## Concurrency

### Web-only path
- RunLock changes from global to per-user. Two users can run web uploads simultaneously even in the same org.
- VPS-disk admission control: before granting a new web upload run, check available disk space. If <5 GB, refuse with "VPS storage full; please use the agent path." (Configurable threshold via env var; default 5 GB.)

### Agent path
- No lock. Each agent runs on its user's local machine.
- Cancel job semantics unchanged.

### Per-org YouTube quota awareness
- Track YouTube API quota usage per org in a `yt_quota_usage` table (org_id, date, units_used).
- Before an upload starts, check projected cost vs daily cap (10K units default). If over, refuse with friendly message.
- This is a soft-lock to prevent silent failures from quota exhaustion; doesn't replace YouTube's own limits.

### Per-org platform soft-lock
- If two members of the same org both try to upload to YouTube at the same time, the second gets "Waiting for [name]'s upload to finish" UI and queues.
- Implementation: per-org per-platform Redis-style mutex stored in SQLite (`platform_locks` table: org_id, platform, locked_by_user_id, locked_at, expires_at). 30-minute expiry for stuck locks.

## Audit log

### Tracked actions
`user.created`, `user.login`, `user.login_failed`, `user.logout`, `user.password_changed`, `user.email_changed`, `user.2fa_enabled`, `user.2fa_disabled`, `user.recovery_requested`, `user.recovery_approved`, `org.created`, `org.disabled`, `org.settings_changed`, `org.member_added`, `org.member_removed`, `org.role_changed`, `invite.sent`, `invite.revoked`, `invite.accepted`, `device.paired`, `device.relinked`, `device.revoked`, `device.renamed`, `secret.connected`, `secret.disconnected`, `upload.started`, `upload.completed`, `upload.failed`, `upload.cancelled`.

### Retention
- Active rows in `audit_log` for 365 days.
- Nightly job moves rows older than 365 days to `audit_log_archive`.
- `audit_log_archive` queryable only by program-owner via admin UI.

## Program-owner admin

`/admin/organizations` (gated by `users.program_owner = TRUE`):
- List all orgs (name, slug, plan, created_at, member count, last activity, disabled?)
- "Create organization" form: org name + Founding Owner email → server creates org + sends invite to that email
- View org detail: members, audit log (full org log), pending invites, devices, secrets (mask-on-display)
- Disable org: blocks login for all members; preserves data
- Search audit log across all orgs (for incident investigation)
- "View as user X" — sets a session flag that lets you see their dashboard for support. Logged as `admin.viewed_as`.

`/admin/users`:
- Cross-org user list
- Force password reset (sends reset email to any user)
- View user's memberships and audit trail

## Resend integration

### Module
`core/email.py`:
- `send(template_name, to: str, **template_vars)` — renders the template, sends via Resend API.
- `RESEND_API_KEY` env var. If missing, email sends are no-ops (with WARNING log), so dev runs don't require API access.
- Retry on transient failures (3 attempts with backoff). Permanent failures logged at ERROR and surfaced in audit log.

### Templates
In `templates/email/`:
- `invite.html`, `invite.txt` — invite email with org name, role, agent download links, accept button
- `password_reset.html`, `password_reset.txt`
- `2fa_code.html`, `2fa_code.txt`
- `recovery_request.html`, `recovery_request.txt` — to Owners when a member submits recovery
- `recovery_approved.html`, `recovery_approved.txt` — to user when an Owner approves
- `login_new_device.html`, `login_new_device.txt` — optional notification on new IP/UA
- `welcome.html`, `welcome.txt` — after signup completes
- `org_created.html`, `org_created.txt` — to program-owner when an org is created (audit copy)

### DKIM/SPF/DMARC
`docs/email-setup.md`:
- Resend dashboard: add `autoalert.pro` domain.
- DNS records for `autoalert.pro` (Cloudflare DNS):
  - DKIM CNAME record (Resend provides specific values)
  - SPF TXT: `v=spf1 include:_spf.resend.com -all` (or `~all` while warming up)
  - DMARC TXT: `v=DMARC1; p=quarantine; rua=mailto:postmaster@autoalert.pro`
- Reverse-DNS check via Resend dashboard.

## Migration to multi-tenant

On first boot of the new code (idempotent):

1. **Schema migration** — run all CREATE TABLEs and ALTER TABLEs. Idempotent (check existing schema before applying).
2. **Bootstrap user** — if no user with `program_owner = TRUE` exists:
   - Create user with `username='admin'`, `email=<PROGRAM_OWNER_EMAIL env var>` (required on first boot or migration aborts with a clear message), `password_hash` derived from the existing `INITIAL_ADMIN_PASSWORD` env var, `program_owner=TRUE`, `password_changed_at=NULL` (forces change on first login).
3. **Bootstrap org** — if no org named "LCBC Church" exists, create it. Set `created_by_user_id=` to the bootstrap user. Add bootstrap user as an Owner via `org_memberships`.
4. **Backfill existing data**:
   - `agent_devices.user_id` = bootstrap user's id (for all existing rows where NULL)
   - `secrets.org_id` = LCBC Church's id (for all existing rows where NULL)
   - `upload_history.org_id` = LCBC Church's id; `upload_history.user_id` = bootstrap user's id
5. **Mark migration complete** — write a `schema_migrations` row.

The bootstrap is idempotent: re-running it is a no-op once migration is complete.

## Agent download UX

### Empty-state card
On `/dashboard` when the current user has zero paired devices, show a large card:
```
┌──────────────────────────────────────────────────────────────┐
│  Get started: download the Daily Life Distributor agent      │
│                                                                │
│  The agent runs on your computer so uploads go directly        │
│  from your machine to the platforms — no double-upload.        │
│                                                                │
│  [ Download for Windows ]   [ Download for macOS ]            │
│                                                                │
│  Detected: Windows  ·  Other downloads & docs                 │
└──────────────────────────────────────────────────────────────┘
```

Auto-detect OS from User-Agent. Highlight the matching button.

### Settings entry
On `/settings/devices`, persistent section at the top:
```
Download the agent  ·  v0.6.0  ·  Released today
[Windows .exe]  [macOS]  ·  Manual install docs
```

### Download URLs
- `https://autoalert.pro/download/agent/windows` → redirects to current Windows binary on the VPS (same path the agent auto-updater uses)
- `https://autoalert.pro/download/agent/macos` → redirects to current macOS binary
- `https://autoalert.pro/download/agent` → OS-detection landing page with both buttons

These wrap the existing `/agent/releases/...` paths so we have stable user-facing URLs even if the release storage path changes.

### Pairing flow tightening
On the download landing page, include a one-time pairing code generated for the current user (so the install → paste-code → done sequence is one continuous flow). Code lives 30 minutes.

## Phasing

Four PRs, each independently deployable and behind feature flags where appropriate.

### PR-α — Schema + users + orgs + new login + admin
- Schema migrations (all tables described above; backward-compatible nullables for existing rows).
- `core/auth.py` rewritten for users (Argon2id) — keeps the shared-password fallback for one release as a safety net (gated by `LEGACY_PASSWORD_ENABLED` env var).
- Login template: username + password.
- Switch-org dropdown (no-op for users in one org).
- Resend integration (`core/email.py`) with template rendering; no emails sent yet (just wired).
- Program-owner admin pages (`/admin/organizations`, `/admin/users`).
- Migration script that creates LCBC Church + bootstrap user.
- Tests for schema, auth, admin pages.

After PR-α merges and is deployed: the program-owner logs in with their migrated password, forced to change it, lands on /admin, can already create new orgs (but no invite flow yet — orgs are empty shells).

### PR-β — Invite flow + role enforcement + permissions
- Invitations table + signed-token issue/redeem.
- `/invite/accept?token=...` signup page.
- `/settings/members` page (list, invite, revoke, role change) gated by role.
- Email templates: invite, welcome.
- Resend actually sends (live API integration).
- Role-permission decorators on every existing route: `@require_role('owner')`, `@require_role('manager')`, etc.
- Per-org credential scoping: `secrets_store` calls now take `org_id` from session.
- Tests for invite send/accept/revoke, role enforcement on each route.

After PR-β: Owners/Managers can invite people; the org has multiple members; per-org credentials work.

### PR-γ — 2FA + recovery + audit log
- TOTP setup wizard, QR code, verification.
- Email 2FA flow.
- Backup recovery codes (generate, download, validate).
- `recovery_requests` flow with Owner approval emails.
- Audit log writer hooked into every action listed above.
- Org-level "Require 2FA" toggle + enforcement.
- Login-from-new-device notification (optional per-user).
- Email templates: 2fa_code, password_reset, recovery_request, recovery_approved, login_new_device.
- Nightly archive job for audit log (>365 days → archive table).
- Tests for full 2FA flow, recovery flow, audit log writes.

### PR-δ — Concurrency rework + agent download + polish
- Lift web RunLock from global to per-user.
- Disk-budget admission control.
- Per-org platform soft-lock (YouTube/Rock/SimpleCast/Vista mutex).
- Per-org YouTube quota tracking.
- Agent download landing page (`/download/agent`).
- Empty-state download card on dashboard.
- Persistent download section in /settings/devices.
- OS auto-detection.
- Pairing code in download URL for one-click setup.
- Tests for new concurrency model.

## Security model

- **Passwords**: Argon2id, never stored plain. Password reset and signup links use signed time-limited tokens.
- **2FA**: TOTP secret encrypted via Fernet (same `SECRET_ENC_KEY` as the existing `secrets_store`). Email 2FA codes hashed in DB, 10-minute expiry.
- **Sessions**: HttpOnly, SameSite=Lax cookies. CSRF protection on state-changing routes (Flask-WTF or equivalent).
- **Authorization**: Every state-changing route checks role + org membership. Default-deny.
- **Audit**: Every privileged action logged with actor, IP, UA.
- **Email enumeration**: "Forgot password" and "create account" forms don't reveal whether an email is registered.
- **Rate limiting**: Login (5/15min/IP), invites (5/hour/Manager), recovery requests (1/24h/user), pair_redeem (5/min/IP — existing).

## Out of scope (deferred for future PRs)

- SSO / OAuth provider login (Google Workspace, Microsoft 365)
- SCIM auto-provisioning
- Custom branding per org (org logo, color theme)
- Per-org compute/storage quotas
- Webhooks for org events
- Detailed billing implementation (Stripe Checkout, plan tiers, usage metering) — the shape is reserved but no code
- SMS 2FA
- Hardware security keys (WebAuthn) — could be added later as a third 2FA option
- Multi-region replication
- Audit log export (CSV download)
- Compliance certifications (SOC 2, etc.)

## Testing

- Unit tests for every model + service (users, orgs, memberships, invitations, recovery, audit).
- Integration tests for the full invite → signup → 2FA → upload flow.
- Migration test: load a snapshot of single-tenant data, run migration, assert LCBC Church exists and owns all the data.
- Email tests: assert correct template renders with correct vars; assert send is called with right address.
- Role-permission tests: matrix of (role, route) → expected status. Generated programmatically.
- Recovery flow test: end-to-end through the approval email.

## References

- Argon2id parameters: OWASP Password Storage Cheat Sheet
- TOTP RFC 6238
- Resend API docs: https://resend.com/docs
- itsdangerous signed tokens
