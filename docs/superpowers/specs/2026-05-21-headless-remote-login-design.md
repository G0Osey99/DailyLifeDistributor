# Install-free remote-browser login for the API-less platforms — design

**Date:** 2026-05-21
**Status:** Approved (design); spec under review
**Branch:** `feat/headless-remote-login`

## Context

The app is moving to a hosted, **headless** Linux VPS (see
`2026-05-21-auth-secret-store-design.md`, Phase 1, now merged). YouTube uses the
Data API and works fully headless. The three **API-less** platforms —
SimpleCast, Vista Social, Rock — authenticate through an **interactive browser
login** that produces session cookies (`storage_state`), which a headless server
cannot perform on its own.

**Constraint from the operator:** authenticating must require **no local
install** and work from **any browser** — ideally built into the web app.

**Why the obvious approach is impossible:** session cookies live on the
*platform's* domain (e.g. `simplecast.com`) and are `HttpOnly`. Same-origin
policy forbids the VPS app (a different domain) from reading them, and
JavaScript/bookmarklets cannot read `HttpOnly` cookies at all. A browser
extension could, but that is an install. Therefore the app cannot harvest an
existing browser login; it must *host* the login itself.

## Decision (locked)

Authenticate via a **self-hosted remote-controlled browser surfaced through the
web app**: the VPS runs a headed Chrome in a virtual display, streams it into a
Settings panel (noVNC), the operator logs in there from any browser with nothing
installed, and the VPS captures the resulting session **server-side** and
encrypts it into the existing secret store.

Rejected alternatives: a third-party remote-browser service (external paid
dependency; church credentials/sessions would transit a third party) and the
local-capture CLI helper (requires the repo + Chrome locally — the install the
operator wants to avoid).

## Architecture / flow

In Settings, each browser platform has a **"Connect"** button. Clicking it:

1. The VPS launches a **headed Chrome inside Xvfb** (virtual display) via
   Playwright, navigated to that platform's login URL.
2. The app **streams the display into a Settings panel via noVNC** (JS VNC
   client served by the app; the operator's browser only renders it). The
   operator logs in there — password, 2FA, etc.
3. The operator clicks **"Save session."** The app calls
   `context.storage_state()`, encrypts the result into the secret store under
   `playwright.<service>`, and tears the browser down.
4. Uploads run **headless** on the VPS using that stored session — unchanged
   from the current uploader behavior.

## Components

### Reused as-is (from Phase 1 / existing code)
- Encrypted session store: `playwright.<service>` blobs and the helpers
  `_persist_session_blob`, `_load_session_blob_to`, `has_session`,
  `clear_session`, `_session_secret_name` in `core/playwright_session.py`.
- Per-service `SessionConfig` (login URL, login-page markers, target URL) in the
  uploaders (`_SC_SESSION_CONFIG_BASE`, `_VS_SESSION_CONFIG`,
  `_ROCK_SESSION_CONFIG`).
- The existing `_handle_login` / `_wait_for_login` login-URL detection (reused
  as a "looks logged in" hint).
- The `no_login_recovery` flag + `SessionExpiredError` (Phase-1 era) for
  non-interactive failure.

### New
- A **remote-login session manager**: starts/stops the Xvfb-backed headed
  browser (single instance), navigates to the service login URL, exposes the
  VNC stream, and on request captures `storage_state` → encrypts → tears down.
  One clear responsibility; parameterized by `SessionConfig`.
- A **Settings panel** embedding the noVNC client + Connect / Save-session /
  Cancel controls and per-service session status (present / absent /
  last-updated).
- Routes: start a remote-login session, the authenticated websocket bridge to
  the VNC server, "save session" (capture/encrypt/teardown), and cancel.

## Login-completion & capture

Capture is driven by an explicit **"Save session" button** — robust, since it
makes no assumptions about 2FA or intermediate pages. The existing login-URL
detection only powers a non-authoritative "looks like you're logged in ✓" hint.

Known limitation: a local password manager will not auto-fill into the remote
browser; the operator types credentials in the streamed view.

## Security

The remote browser holds real platform credentials, so:
- The noVNC page and its websocket bridge are **behind the app's auth gate**
  (only the authenticated operator can reach them) and only over the existing
  HTTPS.
- The VNC server binds to **loopback on the VPS** and is reachable only through
  the authenticated app's proxy — never exposed publicly.
- The remote browser is **started on demand, single-instance, and torn down**
  after Save or an idle timeout — no long-lived browser left running.

## Headless uploads + expiry surfacing

Upload-path `SessionConfig`s run with `no_login_recovery=True` on the VPS (via a
hosted-mode flag), so a missing or expired session raises `SessionExpiredError`
immediately instead of attempting a headed browser that cannot exist. The upload
job surfaces this as **"Session expired for {platform} — click Connect in
Settings to re-authenticate,"** and Settings shows each session's
present/absent + last-updated status.

## Deployment dependency

This requires the VPS to provide **Chrome + Xvfb + a VNC bridge
(x11vnc / websockify) + noVNC**, which pairs naturally with a **Docker image**
(ready-made Chrome+Xvfb+noVNC base images exist). This design therefore assumes
— and likely drives — the Phase 2 "containerized VPS deployment" work; the two
are coupled and should be sequenced together.

## Error handling

- Chrome/Xvfb fails to launch → clear error in the panel ("remote browser
  unavailable; check the VPS Chrome/Xvfb setup"), no half-open session.
- Operator closes the panel without saving / idle timeout → browser torn down,
  no session written.
- `storage_state` capture fails → error surfaced, prior stored session left
  untouched.
- Expired session during a headless upload → `SessionExpiredError` →
  actionable "re-Connect" message (above).
- Concurrent Connect attempts → single-instance lock; second attempt is told one
  is already in progress.

## Testing

- **Unit-testable without a browser:** the session manager's state machine
  (start → connected → save → teardown; idle-timeout teardown; single-instance
  lock), capture-and-encrypt wiring (mock `storage_state` → assert
  `playwright.<service>` blob written + browser torn down), and the
  hosted-mode `no_login_recovery` expiry path (raises `SessionExpiredError`,
  upload job emits the re-Connect message).
- **Auth-gate tests:** the remote-login routes + websocket require an
  authenticated session (401/redirect otherwise).
- **Manual / integration (can't run headless in CI):** the actual noVNC stream
  + interactive login is verified manually on the VPS.

## Phasing

1. **Infra + one service end-to-end** (SimpleCast): Xvfb/noVNC streaming,
   Connect → log in → Save → encrypted session → headless upload uses it.
2. **Add Vista Social + Rock** (mostly `SessionConfig` wiring once the mechanism
   works).
3. **Expiry-surfacing polish** in Settings + upload errors.

## Out of scope

- The Phase 2 VPS deployment/TLS/reverse-proxy itself (coupled, but its own
  effort).
- Migrating SimpleCast/Vista to real APIs (separate, longer-term).
- Automated/credential-replay login (explicitly rejected).
