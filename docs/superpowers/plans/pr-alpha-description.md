# PR-α: Multi-tenant foundation (schema + users + orgs + auth + admin)

## What
Adds the schema, password auth, and program-owner admin pages that the
rest of the multi-tenant work builds on. No behavior change for existing
users until they re-authenticate.

## Scope (PR-α only)
- Schema: organizations, users, org_memberships, invitations,
  recovery_codes, recovery_requests, audit_log, audit_log_archive +
  nullable FK columns on agent_devices, secrets, upload_history.
- Argon2id password hashing.
- Username + password login. Legacy shared-password kept behind
  LEGACY_PASSWORD_ENABLED=true for one release.
- Resend wired (core/email.py) but no live emails sent yet.
- /admin, /admin/organizations, /admin/users (program-owner only).
- Idempotent migration: creates "LCBC Church" org, bootstrap user from
  PROGRAM_OWNER_EMAIL + INITIAL_ADMIN_PASSWORD, assigns existing data
  to it.
- Switch-org dropdown in header (no-op for single-org users).

## Out of scope (PR-β/γ/δ)
- Invites + signup form.
- Role-based authorization on uploads.
- TOTP/email 2FA, recovery codes, recovery requests.
- Audit log writes (table exists, hooks come in PR-γ).
- Concurrency rework + agent download landing page.

## Deploy checklist
1. Set `PROGRAM_OWNER_EMAIL` in .env.
2. Keep `INITIAL_ADMIN_PASSWORD` set for the first boot.
3. Boot once with `LEGACY_PASSWORD_ENABLED=true`, then flip off in
   a follow-up commit once you've confirmed login works.
4. First login forces a password change (password_changed_at=NULL).

## Rollback
Disable the new admin blueprint and set
`LEGACY_PASSWORD_ENABLED=true`. The schema additions are
backward-compatible (all new columns nullable, all new tables unused
by existing code paths).
