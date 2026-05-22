# Hybrid Upload Agent — Design

**Date:** 2026-05-22
**Status:** Approved (brainstorm) — pending implementation plan
**Branch:** `feat/hybrid-upload-agent`

## Problem

Today the browser streams media to the VPS in chunks; the VPS reassembles it
and re-uploads to each platform. Every file therefore crosses the wire twice
(client → VPS, then VPS → platform), and the largest file — the YouTube
video — pays this cost most. We want a faster path where media goes straight
from the user's machine to the platforms, while keeping the existing web-only
flow fully intact as a fallback.

## Goal

A small, self-updating, cross-OS **local agent** that runs the existing
uploaders on the user's machine. The website continues to host the models,
orchestration, history, and the encrypted session store; the agent executes
uploads against **local** media so bytes never transit the VPS. The website
detects a paired agent and offers a "fast upload" path; when no agent is
present, the current web-only chunked path is used unchanged.

## Decisions (from brainstorm)

| Question | Decision |
|----------|----------|
| Scale / users | Small trusted fleet (~1–5 known machines). No app store, no mass code-signing, lightweight pairing. |
| Web ↔ agent transport | **Server-relayed control** over `wss`. Browser and agent each hold a socket to the VPS; the VPS relays small JSON control messages. Media never flows through the relay. |
| Platform credentials | **Pushed per job, transient.** VPS remains the single source of truth; sessions sent over TLS at job start, materialized to a temp file, used, refreshed sessions persisted back, temp file deleted. |
| Agent scope | **All uploaders** — YouTube (Data API resumable) + SimpleCast + Rock + Rock Email + Vista. |
| Media access | **Agent scans configured local folder roots** (reuses `file_scanner.parse_names`) and reports available dates. |
| Agent auth | **Pairing code → revocable per-device token** stored in the OS keychain. |
| Packaging | Single signed per-OS installer; **no prerequisites** (bundles Python + code + Playwright Python pkg). Drives the **system Google Chrome** already required — no bundled browser, small footprint. |
| Auto-update | **First-class.** Publish a build to the VPS once; every agent self-updates. Critical for remote, post-internship maintenance. |

The main alternative considered and rejected was **direct browser ↔ localhost**
(agent runs a local server on `127.0.0.1`). It avoids a control hop but drags
in Private-Network-Access preflight, a local TLS story, and Safari/firewall
edge cases — real friction for marginal gain when the VPS is already present.

## Architecture

```
┌─────────────┐   wss (control only)   ┌──────────────┐   wss (control)   ┌──────────────┐
│   Browser   │◄──────────────────────►│  VPS / Flask │◄─────────────────►│  Local Agent │
│ (website UI)│                         │  + RELAY hub │                   │ (Mac/Windows)│
└─────────────┘                         └──────────────┘                   └──────┬───────┘
                                         session store,                           │ media (direct)
                                         LLM, history,                            ▼
                                         dedup/quota                       ┌──────────────┐
                                                                           │  Platforms   │
                                                                           │ YT/SC/Rock/VS│
                                                                           └──────────────┘
```

### Roles

- **Website (VPS, existing Flask app + new relay).** Unchanged: spreadsheet
  upload/mapping, LLM titles, date/platform selection, `upload_history` /
  dedup / quota, and the encrypted session store (still source of truth).
  New: device pairing, the WebSocket relay hub, a "dispatch to agent" path
  beside the existing server-side `run_batch`, and the auto-update release feed.
- **Relay (on the VPS).** A WebSocket hub that joins a browser session and a
  paired agent (same account) into an account-scoped room and forwards small
  JSON control messages — job plans, progress events, session blobs, log
  lines. **No media flows through it.**
- **Agent (local, cross-OS Python).** Reuses `uploaders/` + `core/` as-is.
  Connects outbound over `wss` with its device token, scans configured media
  roots, runs all uploaders against local files, and streams the same event
  types the dashboard already renders.

### Coexistence

The relay knows whether a paired agent is online. If yes, the dashboard offers
"Fast upload (this device)"; if not, it falls back to the current web-only
chunked path. The same selection UI feeds either path. **Web-only is never
removed.**

## Components

### New on the VPS

- **Pairing + devices.** Generate/redeem one-time pairing codes; an
  `agent_devices` table (`id`, `name`, `token_hash`, `created_at`,
  `last_seen_at`, `revoked`); list/revoke devices in the web UI.
- **Relay hub.** `wss` endpoints for the agent and the browser; account-scoped
  rooms; a JSON message router with heartbeats. Infra: the current sync Flask
  app needs a WebSocket lib (e.g. `flask-sock`); confirm WebSockets pass
  through Cloudflare Tunnel + Caddy (both support `ws`; verify config).
- **Plan builder (agent mode).** Assembles the per-date plan (titles,
  descriptions, footers, schedules), pre-filters with the existing idempotent
  skip (`db.has_successful_upload`), attaches the needed session blobs, and
  sends it to the agent. Mirrors `core.upload_jobs.run_batch` inputs.
- **Result ingestion.** Receives the agent's event stream, writes
  `upload_history` via `db.record_upload`, forwards events to the browser.
- **Release feed.** Serves a signed release manifest (`version`, per-OS URL,
  sha256, signature) for auto-update.

### On the agent (reuses existing code)

- **Transport client** — `wss` connection, reconnect/backoff, device-token
  auth, JSON (de)serialization, protocol-version handshake.
- **Media indexer** — configured roots → `file_scanner.parse_names` →
  available dates reported to the server.
- **Job runner** — adapts `core.upload_jobs` / `_dispatch_upload` to run
  locally against real file paths; reuses every uploader, the per-platform
  circuit breaker, and the email-waits-for-YouTube ordering.
- **Session manager** — receives session blobs per job, materializes temp
  files, persists refreshed sessions back to the VPS store over the relay,
  deletes temp files (the materialize → use → persist → delete pattern, with
  the VPS as the remote store).
- **Local login window** — when a platform session is missing/expired, opens
  headed Chrome on the user's own machine for interactive re-auth (strictly
  better than the VPS headed flow); the resulting session is pushed to the VPS.
- **Updater** — checks the release feed on startup/periodically (or via relay
  push), verifies sha256 + signature against a pinned public key, applies the
  swap, relaunches; on any failure keeps the current version and retries.
- **Minimal tray UI** — status, media-root configuration, pairing-code entry,
  device name.

### Packaging & runtime

- One signed installer per OS (`.msi`/`.exe`, `.dmg`/`.pkg`) built with
  PyInstaller, bundling the Python runtime + `uploaders/`+`core/` + the
  Playwright **Python package** + deps. No system Python, pip, or browser
  install required of the user.
- **No bundled browser** — drives the system Google Chrome
  (`channel="chrome"`) already required by the project, keeping the footprint
  in the tens of MB. If Chrome is absent, the agent offers a **one-time**
  Chromium fetch rather than baking it into every install.
- Targets: Windows + macOS first; Linux optional.

## Auto-update & remote maintainability

- The build pipeline produces a signed per-OS bundle and publishes it to the
  VPS, which serves a signed release manifest.
- The agent checks on startup and periodically; the relay can also push
  "update available" over the open socket. On a newer version it downloads to
  temp, verifies sha256 + a signature against a public key baked into the
  agent, applies the swap (silent installer on Windows / `.app` replace on
  macOS), and relaunches. Any failure leaves the current version running and
  retries later — it never bricks itself.
- **Handoff model:** the remote maintainer publishes a new build to the VPS;
  every agent self-updates with zero action from the team. The maintainer can
  revoke a lost device and read `last_seen` per machine from the web UI.
  First-run for a teammate is: install → paste pairing code → point at media
  folders → done.
- **Signing:** for a small trusted fleet, self-sign and pin our own update key
  (cheap, fully under the maintainer's control). macOS Gatekeeper may need a
  one-time right-click-open on the *initial* install; every auto-update after
  that is seamless. A paid Apple/Windows cert can be added later for wider
  distribution with no redesign.
- **Protocol versioning:** server and agent exchange a protocol version on
  connect; the server can refuse an incompatible agent and prompt it to update.

## Data flow (hybrid happy path)

1. User logs into the website; the relay shows the agent online.
2. Spreadsheet + mapping + LLM titles happen on the VPS (small data, unchanged).
3. Agent reports available dates from its local scan → dashboard shows the
   matched-dates grid.
4. User selects dates + platforms → "Fast upload (this device)."
5. Server builds the plan (idempotent skip applied), attaches session blobs,
   sends to the agent via the relay.
6. Agent uploads local files → platforms, in parallel (reusing the thread pool,
   circuit breaker, and email-after-YouTube ordering), streaming the same
   progress events the dashboard renders today.
7. Per row: agent → server records `upload_history`; refreshed sessions pushed
   back to the store; temp files deleted.
8. Done. Web-only flow remains fully available when no agent is present.

## Security model

- **Transport:** `wss`/TLS end-to-end to the VPS (already behind Cloudflare +
  Caddy).
- **Agent auth:** revocable per-device token (hash on VPS, raw in OS keychain);
  pairing code short-lived and single-use.
- **Session blobs:** already live on the VPS — no new exposure; sent over TLS
  to the authenticated agent for the duration of a job, then the temp file is
  deleted and any refreshed session returned. The agent stores no long-lived
  platform secrets.
- **Update payloads:** signature-verified against a pinned public key.
- **Relay rooms:** scoped to the account; a browser can only reach agents it is
  paired with; device revocation kills the token.

## Error handling / edge cases

- **Agent offline** at selection time → website offers the web-only path.
- **Disconnect mid-job** → run marked interrupted; idempotent skip means a
  re-run (web or agent) only does the rest (same no-resume model as today).
- **Expired platform session** → agent opens local headed login; if the user
  is absent, the row errors and the per-platform circuit breaker trips (same
  semantics as today).
- **Missing media file** for a selected date → per-row error event.
- **Update failure** → keep running the current version; retry next check.
- **Multiple agents** for one account → user targets a chosen device.
- **Protocol skew** → server refuses incompatible agent and prompts update.

## Testing

- **Server:** pairing/token unit tests; relay routing with fake agent + browser
  sockets; plan builder + idempotent-skip tests.
- **Agent:** job runner reuses existing uploader unit tests; transport
  reconnect; session materialize/persist/delete; updater (good signature
  applies, bad signature rolls back).
- **Integration:** a fake relay + stub agent that echoes events asserts the
  browser receives the **same event stream** from the agent path as from the
  server-side path. Real uploaders stay covered by the existing
  (skipped-without-creds) integration tests.

## Suggested phasing (for the implementation plan)

1. Relay + pairing + device tokens — prove browser ↔ agent control round-trips.
2. Agent skeleton: connect, scan media, report dates — **plus the auto-update
   framework early**, so the fleet is self-updating before features stack up.
3. Job dispatch + one platform (YouTube) end-to-end via the agent.
4. All platforms + session push-back + local login.
5. Coexistence UI, fallback, and packaging/signing polish.

## Out of scope (YAGNI)

- App-store distribution and mass code-signing.
- Multi-tenant accounts / self-service signup.
- Locally cached/synced credentials (per-job transient was chosen instead).
- Routing any media through the VPS on the hybrid path.
