# Per-Org Credential Isolation + Owner Impersonation — Design

**Date:** 2026-05-24
**Status:** Approved (brainstorm) — pending implementation plan
**Scope:** Make every per-tenant credential (YouTube OAuth tokens, Playwright session blobs for SimpleCast / Rock / Vista, third-party API keys like Unsplash / Pexels / Resend) belong to a specific organization rather than the process. Add a program-owner impersonation mechanism so the operator can "act as" any org for support and testing without sharing credentials between tenants.

## Problem

The multi-tenant migration (phases α–δ) added organizations, memberships, roles, audit, 2FA, and recovery — but credentials are still process-global. `core/secrets_store.py` has shipped phase-β `org_id` plumbing for 2 weeks, yet **every production caller passes `None`**, hitting the legacy unscoped storage slot. The four hot call sites:

- `uploaders/youtube_uploader.py` (YouTube OAuth token + GCP client_secrets)
- `core/playwright_session.py` (SimpleCast / Rock / Vista storage_state blobs)
- `blueprints/settings.py` (env-style API keys: Unsplash, Pexels, Resend, etc.)
- `core/image_gatherer.py` (`_resolve_key` reading Unsplash/Pexels keys)

Today onboarding a second org would silently overwrite the first org's YouTube token. That blocks the SaaS model.

Separately, when an org's SimpleCast or YouTube login breaks, the program owner has no supported way to log in and re-link those services for them. Today's only option is asking for the user's password — unacceptable.

## Goal

A model where:

- Every credential read/write specifies an `org_id`. Two orgs never see each other's secrets.
- The org for a given request comes from the user's active session, derived from their memberships.
- The program owner can click "Act as <org>" from the admin org page; for the remainder of that session, every credential read writes uses that org's scope, with a persistent banner and an "Exit" button. Real `user_id` never changes.
- `youtube.client_secrets` (the GCP OAuth *client*, not the user *token*) stays a platform-level secret that only the program owner can manage. Every tenant authenticates through the owner's GCP project; only the resulting refresh token is per-org. This avoids forcing every customer to register their own GCP project before they can upload.
- Existing single-tenant secrets in the legacy unscoped slot migrate to the bootstrap org one time, idempotently.
- The agent dispatch path carries the effective org_id in its `job_plan` envelope so secrets ship from the correct scope.

## Decisions (from brainstorm)

| Question | Decision |
|----------|----------|
| How does the current request know its org? | New `core/org_context.py:current_org_id()` returns `session["acting_as_org_id"]` if set, else `session["active_org_id"]`. Set at login (auto for single-membership users, picker for multi-membership users). |
| Impersonation mechanism | Session flag (`acting_as_org_id`). Real `user_id` unchanged. Banner + Exit on every page while active. Only `program_owner` role can set the flag. |
| Audit trail for impersonation | New `audit_log.acting_as_org_id` column. Every write records both `actor_user_id` (real) and `acting_as_org_id` (effective). Start + end of impersonation are themselves audit events. |
| Legacy secrets migration | One-shot copy of every unscoped row to the bootstrap org (the org tied to `PROGRAM_OWNER_EMAIL`), then delete the legacy row. Idempotent: skipped if no legacy rows. |
| Agent dispatch envelope | Server-side `agent_dispatch.start` reads `current_org_id()`, embeds it in the `job_plan` payload. Agent doesn't look up by name — uploaders ship credentials by value in the envelope. |
| Re-auth during impersonation | Allowed. When the owner is acting as org A and re-links SimpleCast via the remote-login flow, the resulting `storage_state` blob is written under org A's scope. Intentional: support use case requires writes, not just reads. |
| `youtube.client_secrets` ownership | Shared platform-level secret. Lives in a new `platform:` namespace (or stays unscoped — see Architecture). Only `program_owner` role sees the settings row to manage it. Per-org `youtube.token` is the per-tenant secret. |
| "Read-only" impersonation mode | Not building. Too easy to get wrong, and the explicit goal is to test, which means writes. |
| Concurrent multi-org session | Not building. One acting org at a time. |

## Architecture

### Storage namespacing

`core/secrets_store.py` already keys storage names like `org:<id>:<name>`. We keep that. Add a new sibling namespace for platform-level secrets:

```
secrets.name
  ├─ <name>                  legacy unscoped (migrated away — empty post-migration,
  │                          except the LEGACY_PASSWORD_ENABLED password hash row)
  ├─ org:<id>:<name>         per-tenant secret
  └─ platform:<name>         platform-shared secret (e.g., the youtube client_secrets blob)
```

`secrets_store` grows `set_platform_secret` / `get_platform_secret` / `set_platform_blob` / `get_platform_blob` — thin wrappers around `_set` / `_get_raw` that produce the `platform:` prefix. Separate functions (rather than overloading the existing `org_id=` arg) because they read more clearly at the call sites and prevent accidentally putting a tenant secret in platform scope.

Exact secret-name constants (`_YT_TOKEN_NAME`, `_YT_CLIENT_SECRETS_NAME`, `_HASH_SECRET`, etc.) live in their respective modules; the implementation plan threads them, this spec just refers to them by role.

### Org context

New module `core/org_context.py`:

```python
def current_org_id() -> int | None:
    """Effective org for the current request.

    Reads session["acting_as_org_id"] if set (program-owner impersonation),
    else session["active_org_id"]. Returns None when no session, which the
    callers treat as 'fail closed' (no creds available).
    """

def is_impersonating() -> bool: ...
def real_user_id() -> int | None: ...    # always session["user_id"]
def effective_user_id() -> int | None: ...  # same as real today; reserved for future user-impersonation
```

This is the single source of truth. Every credential call site goes through it.

### Session shape after this change

```python
session = {
    "user_id": 7,                  # always real
    "active_org_id": 3,            # the user's selected membership org
    "acting_as_org_id": 11,        # optional; set only by /admin/.../act
    "permission_2fa_passed": True,
    # ...
}
```

Login flow:
- 1 membership → `active_org_id` auto-set to that org.
- N memberships → after auth + 2FA, redirect to `/login/select-org` showing the user's orgs.
- Owner can also pick across their own memberships from a header dropdown (independent of impersonation).

### Audit log

```sql
ALTER TABLE audit_log ADD COLUMN acting_as_org_id INTEGER;
ALTER TABLE audit_log_archive ADD COLUMN acting_as_org_id INTEGER;
```

`core/audit.write_event()` grows an implicit lookup: if not given, reads `org_context.session.get("acting_as_org_id")`. New action codes: `impersonation.start`, `impersonation.end`.

### Impersonation lifecycle

- `POST /admin/organizations/<id>/impersonate`
  - 403 unless caller has role `program_owner`.
  - Sets `session["acting_as_org_id"] = <id>`.
  - Writes `audit_log` row: `actor_user_id=<owner>`, `action="impersonation.start"`, `acting_as_org_id=<id>`.
- `POST /admin/exit-impersonation`
  - Clears the key.
  - Writes `impersonation.end`.
- Global Jinja context processor renders the banner whenever `acting_as_org_id` is in the session. Banner contains org name + Exit form.

### Forbidden-during-impersonation routes

The owner *acting as* org A must not be able to weaken their own account or org A's account hygiene. Hard block (HTTP 409 + message) when `is_impersonating()`:

- `/settings/2fa/disable`
- `/settings/2fa/enable-totp` / `enable-email`
- `/recover/admin-approve/...`
- Any password-change route
- Any member role-change route (we'd be acting on their org's membership graph as them, which is wrong)

Acceptable while impersonating: everything an org owner does in the *normal* workflow — upload media, manage YouTube/Simplecast/Rock linkage, view their history, edit org-level settings that aren't security-critical.

### Credential plumbing — call site changes

| File | Before | After |
|---|---|---|
| `uploaders/youtube_uploader.py` | `secrets_store.get_secret(_YT_TOKEN_NAME)` | `secrets_store.get_secret(_YT_TOKEN_NAME, org_id=current_org_id())` |
| `uploaders/youtube_uploader.py` (client_secrets) | `secrets_store.get_secret(_YT_CLIENT_SECRETS_NAME)` | `secrets_store.get_platform_secret(_YT_CLIENT_SECRETS_NAME)` |
| `core/playwright_session.py` | `secrets_store.get_blob(_session_secret_name(f))` | `secrets_store.get_blob(_session_secret_name(f), org_id=current_org_id())` |
| `blueprints/settings.py` (env API keys) | `secrets_store.set_secret(name, value)` | `secrets_store.set_secret(name, value, org_id=current_org_id())` |
| `blueprints/settings.py` (`youtube.client_secrets` upload) | open to any authenticated user | gated by `require_program_owner`; reads/writes through `set_platform_blob` |
| `core/image_gatherer.py:_resolve_key` | reads from unscoped slot then env | reads `get_secret(name, org_id=current_org_id())` first, then env |
| `core/auth.py:_HASH_SECRET` | unchanged (legacy shared password) | unchanged — this is the LEGACY_PASSWORD_ENABLED gate, not a tenant credential |

The agent path:
- `core/agent_dispatch.start` resolves `current_org_id()` once at dispatch time, attaches it to the `job_plan` envelope as `org_id`.
- Agent doesn't change behavior: it still receives a credentials snapshot by value via `agent/secrets_shim` and uses it. The shim already lives in-memory per job; it never reads from any disk store.

### Legacy migration

In `core/migration_bootstrap.run_migration()` (already idempotent), add a one-time legacy-secrets pass:

1. If no legacy unscoped secret rows exist → skip.
2. Else, resolve the bootstrap org (the one created from `PROGRAM_OWNER_EMAIL`) once.
3. For each legacy row whose name is NOT the legacy shared-password hash (`core/auth.py:_HASH_SECRET`) and NOT the GCP client_secrets blob:
   - `set_secret(name, decrypted_value, org_id=bootstrap_org_id)` (or `set_blob` for blobs).
   - Delete the legacy row.
4. GCP client_secrets (`uploaders/youtube_uploader.py:_YT_CLIENT_SECRETS_NAME`) → move to `platform:<that-name>`. Delete the legacy row.
5. Legacy shared-password hash → leave in place; only meaningful when `LEGACY_PASSWORD_ENABLED=true`, and it's not a tenant credential.

Single SQLite transaction. Logged. Audit-logged once as `system.legacy_secret_migration` with the row count.

## Phasing

| Phase | Scope | Files touched | Tests |
|---|---|---|---|
| **1. Org context + audit column** | `core/org_context.py` with `current_org_id()`. `audit_log.acting_as_org_id` column + archive table. `core/audit.write_event()` auto-fills from session. Login picker for multi-membership users. | `core/org_context.py` (new), `core/db.py` (DDL), `core/audit.py`, `blueprints/auth.py`, `templates/login_select_org.html` (new) | `test_org_context.py`, `test_audit_acting_as_org.py` |
| **2. Secret namespace + platform wrappers** | Add `set_platform_secret` / `get_platform_secret` / blob counterparts. Add the call-site grep audit script to CI to prevent unscoped regressions. | `core/secrets_store.py`, `scripts/check_secret_scoping.py` (new) | `test_secrets_store.py` (extend), `test_secret_scoping_lint.py` |
| **3. Plumb call sites** | Every prod call site switches to `org_id=current_org_id()`. `youtube.client_secrets` switches to platform store and `require_program_owner` gating on settings. | `uploaders/youtube_uploader.py`, `core/playwright_session.py`, `blueprints/settings.py`, `core/image_gatherer.py` | `test_youtube_per_org_token.py`, `test_playwright_session_per_org.py`, `test_settings_admin_only_client_secrets.py` |
| **4. Impersonation UI + guards** | `/admin/organizations/<id>/impersonate`, `/admin/exit-impersonation`, banner partial, forbidden-route guard list, audit events. | `blueprints/admin.py`, `blueprints/impersonation.py` (new), `templates/_impersonation_banner.html` (new), context processor in `app.py` | `test_impersonation_flow.py`, `test_forbidden_during_impersonation.py` |
| **5. Agent dispatch + migration** | `agent_dispatch.start` reads `current_org_id()`, embeds in `job_plan`. `migration_bootstrap` runs the legacy migration block. | `core/agent_dispatch.py`, `core/migration_bootstrap.py`, agent path doesn't change (shim already by-value) | `test_agent_dispatch_org_scope.py`, `test_legacy_secret_migration.py` |
| **6. Cross-org isolation hardening** | Acceptance tests: two orgs each with their own YouTube token; upload from org A pulls org A's token; impersonation correctly swaps. Forbidden routes enforced. | `tests/integration/test_cross_org_isolation.py` (new) | as listed |

## Test acceptance

The feature is done when all of the following pass:

1. **Cross-org credential isolation.** Org A and org B each set a distinct `youtube.token`. A user in org A triggers an upload → `youtube_uploader` receives A's token. A user in org B triggers an upload → receives B's token. Cross-reads return None.

2. **Owner impersonation reads target-org creds.** Owner is a member of bootstrap org. Owner clicks "Act as org B" → uploads a video → the YouTube token used is B's. Audit row shows `actor_user_id=<owner>`, `acting_as_org_id=<B>`.

3. **Re-auth during impersonation lands in target scope.** Owner acts as org B, opens remote-login for SimpleCast, completes login → resulting `storage_state` blob is stored under org B's scope. Exiting impersonation does not move the blob.

4. **`youtube.client_secrets` is admin-only.** Org owner (not program owner) visiting `/settings` does not see the client_secrets upload row. The route returns 403 if posted to.

5. **Legacy migration is idempotent.** On first run with N legacy rows, exactly N rows move to bootstrap org's scope + the platform scope for client_secrets. Second run: no-op.

6. **Forbidden routes blocked under impersonation.** All listed 2FA/password/recovery routes return 409 with a helpful message while `acting_as_org_id` is set; succeed when cleared.

7. **Agent dispatch carries org_id.** Dispatching from a session where `acting_as_org_id=B` produces a `job_plan` envelope with `org_id=B`. The agent worker uses B's credentials via the by-value shim.

## Non-goals (YAGNI)

- Per-user (intra-org) credentials. Orgs share their own creds among their members by design.
- Concurrent multi-org active sessions for the same user.
- Read-only impersonation mode.
- User impersonation (acting as a specific user, not an org). Reserved field `effective_user_id()` exists for the future but is not implemented.
- Re-encrypting at rest with a per-org KEK. Master key (`SECRET_ENC_KEY`) stays single — the per-org separation is at the storage-name level.
- Migrating away from SQLite for the secrets table.
- Stripe/billing tie-ins per org.

## Open implementation questions (for the plan, not for this spec)

- Exact UI placement of the org picker for multi-membership users (header dropdown vs. dedicated route). The login_select_org template handles the post-login picker; in-flight switching is a UX decision the implementation plan resolves.
- Whether the legacy migration runs *before* or *after* phase 3's call-site plumbing lands. Either order works — phase 3 with no migration leaves orgs with empty scopes (forces re-upload of creds via settings); migration with no plumbing has no effect. The phasing table runs plumbing first to get rapid feedback in dev, migration last to ship cleanly.
