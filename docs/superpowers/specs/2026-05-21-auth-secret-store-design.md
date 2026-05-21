# Phase 1 — Authentication gate + encrypted secret store

**Date:** 2026-05-21
**Status:** Approved (design); spec under review
**Branch:** `feat/auth-secret-store`

## Context & direction change

The app is moving from a **loopback-only local web app** to a **hosted web app
on a VPS**. Today the only access control is the loopback guard in `app.py`
(`before_request` 403s any request whose `remote_addr` isn't `127.0.0.1`, plus a
Host-header allowlist) — which would 403 *everyone* on a VPS. Secrets currently
live in `.env` plus loose JSON files, which doesn't survive redeploys and isn't
encrypted.

This is **Phase 1** of the migration. Later phases (VPS deployment/TLS,
swapping browser uploaders for API-key ones) are out of scope here but recorded
at the end.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Auth model | **Single shared credential** (no per-user accounts) |
| Master key custody | **App-held key in a VPS env var**; AES (Fernet) encryption at rest in the DB |
| Phase 1 scope | **Auth gate + encrypted store + migrate ALL secrets** (KV keys *and* file blobs) |

## Goal

A first-timer deploying this can: set two env vars (`SECRET_ENC_KEY`,
`INITIAL_ADMIN_PASSWORD`), start the app, log in, and find that **no secret
sits in plaintext at rest** — every API key, OAuth token, and browser session
is encrypted in `state.db`, managed through the Settings UI.

## Components

### 1. Access control — replace the loopback guard (`app.py`)
- **Remove** the loopback/Host `before_request` guard.
- **Add** an auth gate: every route except `/login`, `/health`, and static
  assets requires an authenticated session. Unauthenticated → redirect to
  `/login`; for XHR/JSON requests → `401`.
- **Keep** the existing `Origin`/`Sec-Fetch-Site` CSRF check, but drive its
  allowed origin from a configurable **`ALLOWED_HOSTS`** (the VPS domain)
  instead of hardcoded loopback — preserving DNS-rebind/CSRF defense in the
  hosted context.
- Session = Flask signed cookie with `HttpOnly`, `SameSite=Lax`, and `Secure`.
  `Secure` is toggleable (`SESSION_COOKIE_SECURE`, default on) so local
  http-dev still works. TLS termination itself is a later deployment concern.

### 2. Authentication — `core/auth.py` + `blueprints/auth.py`
- Single shared credential. Password **hash** stored in the DB using
  `werkzeug.security` scrypt (no new dependency).
- **Bootstrap:** on first boot, if no credential exists, seed the hash from the
  `INITIAL_ADMIN_PASSWORD` env var. Env-seed (not a public setup page) avoids
  the race where an attacker on the open VPS claims the password first.
- Routes: `GET /login` (render form), `POST /login` (submit), `POST /logout`
  (CSRF-safe). A `login_required` gate. Per-IP
  failed-attempt backoff/lockout against brute force. Generic error messages
  (no account enumeration; single credential anyway).
- Password is changeable via the Settings UI (re-hash, store).

### 3. Crypto core — `core/crypto.py`
- Master key from env var **`SECRET_ENC_KEY`** (a 32-byte urlsafe-base64 Fernet
  key).
- **Fail closed:** missing/invalid key → the app refuses to start and prints
  the one-liner to generate one
  (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
- API: `encrypt(bytes) -> token`, `decrypt(token) -> bytes`, using the
  `cryptography` library's **Fernet** (authenticated AES — built-in nonce and
  tamper detection). `cryptography` is the **one new dependency**.

### 4. Encrypted secret store — `core/secrets_store.py` + `secrets` table
- New `state.db` table:
  `secrets(name TEXT PRIMARY KEY, kind TEXT, value BLOB, updated_at TEXT)`,
  with `kind ∈ {kv, blob}` and `value` = Fernet-encrypted bytes.
- API:
  - KV: `set_secret(name, plaintext)`, `get_secret(name) -> str | None`,
    `delete_secret(name)`, `list_secret_names()`.
  - Blob: `set_blob(name, data: bytes)`, `get_blob(name) -> bytes | None`.
  - `materialize_blob_to_tempfile(name)` — a **context manager** that decrypts a
    blob to a `0600` temp file, yields its path, and shreds it on exit. This is
    how Google libs / Playwright keep reading "files" while nothing sits in
    plaintext at rest.
- `state.db` is already gitignored, so encrypted secrets never reach git.

### 5. Migrate ALL secrets into the store
- **KV API keys** (`UNSPLASH_ACCESS_KEY`, `PEXELS_API_KEY`, future
  SimpleCast/Vista/Rock keys): switch call sites from `os.environ.get(...)` to
  `secrets_store.get_secret(...)`, with a **temporary env fallback** for the
  transition window.
- **YouTube** `client_secrets.json` + `token.json`: stored as encrypted blobs;
  `uploaders/youtube_uploader.py` materializes them to temp files for the OAuth
  flow and writes the **refreshed token back encrypted** after use.
- **Playwright** `simplecast_session.json` / `vista_social_session.json` /
  `rock_session.json`: `core/playwright_session.py` materializes from an
  encrypted blob and **re-encrypts** the updated cookies after each login.
- **One-time importer** — `scripts/migrate_secrets.py`, also auto-run on boot
  when the store is empty and plaintext files/env vars are present. Idempotent;
  logs exactly what it imported so the operator can flip over without
  re-entering everything, then delete the plaintext.

### 6. Settings UI (`templates/settings.html` + `blueprints/settings.py`)
- A **Secrets** panel: each secret shows **set/unset status only** (never echoes
  the stored value), with a replace field and a clear button.
- YouTube keeps its authorize flow (token now stored encrypted).
- Playwright sessions show status + a clear button.
- A **Change password** control.

## Data flow

1. **Boot:** load master key from env (fail closed) → init DB (create `secrets`
   table) → auto-import plaintext secrets if store empty and sources present →
   seed admin password from `INITIAL_ADMIN_PASSWORD` if no credential.
2. **Request:** `before_request` → authenticated? If not and not an allowlisted
   route, redirect to `/login` (or `401` for XHR).
3. **Login:** verify password against the stored hash → set session cookie.
4. **Using a secret:** call site asks the store → Fernet-decrypts with the
   master key; file-based secrets are materialized to a temp file via the
   context manager.
5. **Token/session refresh:** updated YouTube token / Playwright cookies are
   re-encrypted and written back to the store.

## Error handling

- Missing/invalid `SECRET_ENC_KEY` → refuse to start with generate-key
  instructions.
- Decrypt failure (wrong key / tampered ciphertext) → treat that secret as
  unset and log loudly; never crash the whole app.
- No credential and no `INITIAL_ADMIN_PASSWORD` → fail closed with setup
  instructions.
- Failed logins → per-IP backoff/lockout; generic error.

## Testing

- **crypto:** encrypt/decrypt round-trip; wrong-key rejection; tamper
  detection.
- **secrets_store:** set/get/delete/list; blob round-trip;
  `get_secret` of an unset name returns `None`; `materialize_blob_to_tempfile`
  creates then removes the temp file.
- **auth:** login success/failure; lockout after N attempts; logout clears
  session; `login_required` redirects the unauthenticated; `/health` and
  `/login` reachable without a session.
- **migration:** importing plaintext files/env populates the store; running it
  twice is a no-op (idempotent).

## New / changed config & env

| Name | Where | Purpose |
|---|---|---|
| `SECRET_ENC_KEY` | env (required) | Fernet master key; app fails closed without it. |
| `INITIAL_ADMIN_PASSWORD` | env (first boot only) | Seeds the shared login credential. |
| `ALLOWED_HOSTS` | env/config | Comma-separated hostnames the CSRF/host check accepts (the VPS domain). |
| `SESSION_COOKIE_SECURE` | env/config | Default on; toggle off for local http-dev. |
| `cryptography` | `requirements.txt` | New dependency for Fernet. |

## Out of scope (recorded for later phases)

### VPS / deployment (later)
TLS via a reverse proxy (Caddy/nginx), Dockerization, process supervision.

### Uploader strategy — **API-first, Playwright as explicit fallback** (later)
On a VPS the harder problem with browser automation isn't monthly cookie expiry
— it's the **interactive headed login** the current uploaders rely on, which a
headless server can't provide. So:

| Platform | Direction | Why |
|---|---|---|
| YouTube | **API** (already) | Stable, OAuth in place; keep. |
| Rock | **Move to API** | Rock has a solid REST API (recon already done); drops the Playwright session entirely. |
| SimpleCast | **Likely stay Playwright** | Its REST API was deliberately abandoned (audio-encoding/scheduling edge cases). |
| Vista Social | **Likely stay Playwright** | Publishing API is partner/approval-gated. |

Architecture stays a uniform uploader interface with two backend kinds (API
client / browser driver) selected per-platform in config. For the Playwright
cases, the **Phase 1 encrypted store already solves cookie refresh cleanly**:
log in **locally** with a small helper to produce the `storage_state`, then
upload that session blob through the Settings UI (encrypted at rest) — no
VNC/Xvfb on the server. Monthly refresh = "run local helper, upload new
session," not "remote into the VPS."

### Other (later)
Per-user accounts, roles, and audit logging.

## Phasing

1. **Phase 1 (this branch):** auth gate + encrypted secret store + migrate all
   secrets.
2. **Phase 2:** VPS deployment + TLS/reverse proxy.
3. **Phase 3:** Rock → API; formalize the API-vs-browser uploader interface +
   local-login session-refresh helper.
