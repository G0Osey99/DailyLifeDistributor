# Hybrid Upload Agent — Phase 3 Design

**Date:** 2026-05-22
**Status:** Approved (brainstorm) — pending implementation plan
**Parent spec:** `docs/superpowers/specs/2026-05-22-hybrid-upload-agent-design.md`
**Prior phases:** Phase 1 (relay + pairing), Phase 2a (media scan), Phase 2b (auto-update) — all merged.
**Gating:** `HYBRID_AGENT_ENABLED=true` (already set on the VPS).

## Problem

Phases 1–2b proved the foundation: paired agents, control over `wss`, local
media scan, and a self-update pipeline. None of that is doing useful upload
work yet. Phase 3 delivers the payoff: real platform uploads run on the local
agent against local media, with the same event stream and `upload_history`
durability the web-only path offers today.

## Goal

When a paired agent is online and the user picks "fast upload," the server
builds a per-date job plan, attaches platform session blobs, and sends them
over the existing relay. The agent runs the real uploaders (`youtube`,
`simplecast`, `rock/*`, `vista_social`) against its local files, streams the
existing event types back, and the server records `upload_history` plus any
refreshed session blobs — exactly what `core.upload_jobs.run_batch` does
today, just executed on the user's machine instead of the VPS. The web-only
path stays fully functional and is the fallback whenever no agent is online.

## Constraints carried from prior phases

- Agent's only direct `core/` import today is
  `core.file_scanner.parse_names`. Phase 3 adds a direct dependency only on
  `core.circuit_breaker` (used by `agent/run_batch.py`) and a new
  `RemotePlaywrightSession` shim in `agent/`. `agent/run_batch.py` does
  **not** call into `core.config` — it reads its runtime config from the
  envelope instead of loading `config.yaml`.
- `uploaders/*` keep all the `core/` imports they have today. The agent's
  own code does not call them directly. Two of these uploader imports
  (`core.secrets_store` from `youtube_uploader.py`, `core.db` from
  `rock/orchestrator.py`) reach state the agent does not have, so the agent
  installs **symmetric stub modules** at startup (see `agent/secrets_shim.py`
  and `agent/db_shim.py` below) that translate calls into envelope reads
  and outbound events.
- **Not bundled / never imported by the agent:** `core.llm_title_gen`,
  `core.upload_jobs`. (`core.db` and `core.secrets_store` are bundled because
  uploaders import them at module level, but they're shadowed by stubs at
  agent runtime — see below.)
- The existing relay (`blueprints/agent.py`) and `wss` plumbing are reused.
  Protocol-version handshake from Phase 1 gates incompatible agents.
- Everything new is behind `HYBRID_AGENT_ENABLED`. Web-only path code remains
  untouched in its primary form.

## Architectural decisions

| # | Question | Decision |
|---|----------|----------|
| 1 | Where does per-row orchestration live for the agent path? | Server pre-bakes plan; slim agent runner. Agent never imports `db`, `secrets_store`, `llm_title_gen`, or `core.upload_jobs`. |
| 2 | Where does the server-side dispatcher live? | New `core/agent_dispatch.py` mirroring `core.upload_jobs.run_batch` inputs but emitting a JSON envelope. `/upload` routes to it via a `path=agent` flag set by the dashboard chip. |
| 3 | How is the agent's orchestration loop implemented? | New `agent/run_batch.py` — copy-and-trim of `run_batch` minus the two `db.*` calls. Drift accepted; extract to shared module later if it bites. |
| 4 | How do session blobs and OAuth credentials transport? | One `credentials` map inline in the `job_plan` envelope, keyed by the same string `secrets_store` uses (e.g. `youtube.token`, `youtube.client_secrets`, `simplecast.session`, `rock.session`, `vista_social.session`). On the agent, `agent/secrets_shim.py` shadows `core.secrets_store` and reads/writes against an in-memory dict + tempdir backed by this map. Mutations emit `credentials_updated{key, value}` events — server writes them back to the real `secrets_store`. `RemotePlaywrightSession` is a context-manager wrapper around the same shim. |
| 4a | How does `core.db.record_image_use` work on the agent? | `agent/db_shim.py` shadows `core.db` and forwards `record_image_use` (and `append_credits_entry`) calls as `image_used` events. Server-side handler applies them to the real `db.record_image_use` and credits file. |
| 5 | How do file paths round-trip? | Server strips path fields from serialized `ReviewEntry` before send. Agent re-resolves `(date, media_kind) → local path` from its Phase 2a scan map. Scan coverage of all media kinds (including `email_thumbnails`) verified during planning; extended if gaps. |
| 6 | How is agent ↔ VPS disconnect handled mid-run? | Agent keeps in-flight uploads running. Bounded in-memory event buffer replayed on reconnect within the window. Hello frame carries `pending_results: [...]` on every reconnect; server idempotently applies via `db.record_upload`. Rows lost when the agent process dies fall to idempotent-skip on re-run. |
| 7 | How does the browser reattach? | Server drops buffered events for a missing browser. Dashboard reads state from `upload_history` on reload and re-subscribes to live events. |
| 8 | How does the UI choose agent vs web-only? | Single Upload button plus a small chip near it: `via agent: <device-name>` with a "use web instead" link. Default to agent when one is online. Multi-device picks the most-recently-seen. |
| 9 | Local headed login on the agent for expired sessions? | Deferred. Phase 3 errors the row on `SessionExpiredError`; user re-auths via the existing web-only login UI, refresh flows to the agent on the next envelope. |
| 10 | Phase 5 UI in this round? | Minimum chip + override lands in Phase 3 (only way the agent path is end-to-end testable from the real UI). Device-management UI (rename/revoke/picker) deferred. |

## Architecture

```
┌──────────────┐  /upload?path=agent  ┌─────────────────────────────────┐
│ Dashboard JS │─────────────────────►│ Flask                           │
│              │  SSE /upload/stream  │  /upload selects on path flag:  │
│ [Upload]     │◄─────────────────────┤    path=agent → agent_dispatch  │
│  via agent…  │                      │    path=web   → upload_jobs     │
└──────┬───────┘                      │                                 │
       │ /agent/ws (browser socket)   │  core/agent_dispatch.py (new):  │
       └─────►┌──────────────────┐    │   • filter has_successful_upload│
              │ relay hub        │◄──►│   • pull credentials            │
              │ (Phase 1)        │    │   • build envelope              │
              └────────┬─────────┘    │   • route events → SSE queue    │
                       │ wss          │   • on success → record_upload  │
                       ▼              │   • on credentials_updated →    │
                                      │     secrets_store.set_secret    │
                                      │   • on image_used → db +        │
                                      │     image_gatherer              │
              ┌─────────────────────────────────────────────────────────┐
              │ Agent (PyInstaller bundle)                              │
              │                                                          │
              │  agent/dispatch.py (new) — control plane                │
              │   • receives job_plan; materializes sessions to tempdir │
              │   • spawns agent/run_batch.run(envelope, emit)          │
              │   • buffers events; replays on reconnect                │
              │   • pending_results in hello frame                      │
              │                                                          │
              │  agent/run_batch.py (new) — orchestration               │
              │   • thread pool (config.upload.max_workers)             │
              │   • per-platform circuit breaker                        │
              │   • email-waits-for-YouTube ordering                    │
              │   • dispatches to uploaders/* against local paths       │
              │                                                          │
              │  uploaders/* (bundled, unchanged source)                │
              │  agent/secrets_shim.py installed as core.secrets_store  │
              │  agent/db_shim.py installed as core.db                  │
              │  RemotePlaywrightSession (tempdir context mgr on shim)  │
              └────────────────────────┬────────────────────────────────┘
                                       │ direct media upload
                                       ▼
                          ┌─────────────────────────┐
                          │ YT / SC / Rock / Vista  │
                          └─────────────────────────┘
```

## Components

### New on the VPS

- **`core/agent_dispatch.py`** — server-side dispatcher. Public entry
  `start(job_id, dates, summary, session_id, entries_snapshot, ...) → None`
  mirrors `core.upload_jobs.run_batch`'s input shape. Responsibilities:
  - Apply `db.has_successful_upload` to drop already-done rows from the
    envelope (so the agent only sees rows that need work).
  - Pull every needed credential from the encrypted `secrets_store`
    (Playwright session blobs + YouTube OAuth token + YouTube client
    secrets) and bundle them into the envelope's `credentials` map.
  - Build the `job_plan` envelope (see protocol). Strip path fields from
    serialized `ReviewEntry` dicts before send.
  - Send the envelope to the chosen agent via the relay (most-recently-seen
    online device for the account).
  - Listen for incoming frames from the relay's queue and pump them onto
    the same per-job `queue.Queue` that the SSE endpoint already reads
    (so `static/js/dld_pipeline.js` sees identical events).
  - On `success` events, call `db.record_upload`.
  - On `credentials_updated{key, value}` events, persist back via
    `secrets_store.set_secret`.
  - On `image_used{...}` events, call `db.record_image_use` and
    `core.image_gatherer.append_credits_entry`.
- **`/upload` route changes** — accept a `path` parameter (`agent` or `web`,
  default `web`). On `path=agent`, dispatch to `agent_dispatch.start` instead
  of `upload_jobs.run_batch`. Behind `HYBRID_AGENT_ENABLED`; otherwise the
  parameter is ignored and the web-only path runs.
- **`pending_results` ingestion** — `blueprints/agent.py` socket handler
  recognizes a `pending_results` field on the agent's hello frame, idempotently
  applies entries (dedup key `(job_id, row_idx, platform)`) to
  `upload_history`, forwards to any attached browser, and acks so the agent
  can clear its buffer.
- **Dashboard chip** — `static/js/dld_pipeline.js` shows a small chip near
  the Upload button reflecting whether an agent is online (data already
  available from the existing browser socket presence events) and the chosen
  path. Default to agent when present; "use web instead" link flips a local
  flag that's passed as `path=` to `/upload`.

### New on the agent

- **`agent/dispatch.py`** — control plane. Receives `job_plan` frames from
  `agent/transport.py`. For each plan:
  - Installs `secrets_shim` and `db_shim` as `core.secrets_store` /
    `core.db` in `sys.modules` (idempotent — installed once at agent
    startup; the dispatch path just (re)loads the shim's backing dict from
    the envelope's `credentials` map for this job).
  - Resolves `(date, media_kind) → local_path` for each row using the cached
    Phase 2a scan results.
  - Spawns `agent/run_batch.run(...)` with a transport-aware `emit` callback.
  - Buffers outgoing `event`, `credentials_updated`, and `image_used`
    frames in memory. On transport disconnect, keeps `run_batch` running.
    On reconnect within the bounded window, replays the buffer.
  - Maintains a durable (in-memory for Phase 3 — process-bounded) list of
    `pending_results` that gets sent in every reconnect's hello frame and
    cleared on server ack.
- **`agent/run_batch.py`** — orchestration. Copy-and-trim of
  `core.upload_jobs.run_batch`:
  - Thread pool sized by `config.upload.max_workers` (from a small config the
    envelope carries — agent does not load `core.config` from a yaml file).
  - Per-platform circuit breaker via `core.circuit_breaker` (bundled).
  - Email-waits-for-YouTube ordering preserved verbatim.
  - Per-platform dispatch calls into `uploaders/*` with local file paths.
  - `db.has_successful_upload` removed (skip pre-applied server-side).
  - `db.record_upload` removed (server records on receiving `success` events).
- **`agent/secrets_shim.py`** — installed into `sys.modules` as
  `core.secrets_store` at agent startup, before any uploader import.
  Backed by an in-memory dict (the envelope's `credentials` map) and a
  per-run tempdir. Implements `get_secret`, `set_secret`, `delete_secret`,
  and `materialize_blob_to_tempfile`. Any mutation (e.g. YouTube refreshes
  its OAuth token) emits a `credentials_updated{key, value}` event so the
  server-side `secrets_store` stays the source of truth.
- **`agent/db_shim.py`** — installed into `sys.modules` as `core.db` at
  agent startup. Implements only what the bundled uploaders touch:
  `record_image_use(...)` (forwards as an `image_used` event). Everything
  else raises a clear `NotImplementedError` so accidental future coupling
  surfaces loudly. The agent never reads/writes a SQLite file.
- **`RemotePlaywrightSession` shim** (in `agent/`) — context manager mirroring
  the `PlaywrightSession.__enter__/__exit__` API used by Rock/SC/Vista
  uploaders. Built on top of `secrets_shim`: on enter writes the supplied
  blob to a tempdir-scoped path and exposes that path; on exit hashes the
  file contents, calls `secrets_shim.set_secret` (which routes the change
  through `credentials_updated`) if the hash changed, deletes the temp file.
- **Scan coverage check** — the implementation plan verifies the Phase 2a
  scan indexes every directory the uploaders need (YouTube video, Shorts,
  YouTube thumbnails, **email thumbnails** per CLAUDE.md, SimpleCast audio,
  Rock spotlight/vista/reflection assets). Any missing kind is added in
  Phase 3.

## Wire protocol additions

Over the existing wss relay. All frames JSON. Protocol-version handshake from
Phase 1 unchanged.

**Server → Agent**

```json
{
  "type": "job_plan",
  "job_id": "<uuid>",
  "protocol_version": 1,
  "config": { "max_workers": 4, "circuit_breaker": { "failure_threshold": 3, "recovery_timeout_seconds": 60 } },
  "rows": [
    {
      "row_idx": 0,
      "iso_date": "2026-05-22",
      "platforms": ["YouTube Video", "Rock", "Rock Email", "Simplecast"],
      "entry": { ...serialized ReviewEntry with path fields stripped... },
      "elements": { ...UploadElements... }
    }
  ],
  "credentials": {
    "simplecast.session": "<JSON blob>",
    "rock.session": "<JSON blob>",
    "vista_social.session": "<JSON blob>",
    "youtube.token": "<JSON blob>",
    "youtube.client_secrets": "<JSON blob>"
  }
}
```

Keys in `credentials` match the strings `core.secrets_store` already uses
on the server, so the agent's `secrets_shim` is a drop-in. Path fields
stripped per decision #5; the agent re-resolves locally.

A `cancel_job{job_id}` message type is reserved but not implemented in
Phase 3.

**Agent → Server**

- `event` — payload identical to the SSE event shapes already emitted by
  `core.upload_jobs.run_batch`:

  ```json
  { "type": "event", "job_id": "...", "row_idx": 0, "event": "upload_progress",
    "platform": "YouTube Video", "percent": 42.5, "bytes_sent": ..., "bytes_total": ... }
  ```

  Event names: `start`, `upload_progress`, `phase_change`, `processing_start`,
  `processing_done`, `success`, `error`, `skip`, `done`.

- `credentials_updated`:

  ```json
  { "type": "credentials_updated", "job_id": "...",
    "key": "rock.session", "value": "<JSON blob>" }
  ```

  Emitted whenever the `secrets_shim`'s in-memory value for a key changes
  (e.g. `RemotePlaywrightSession.__exit__` detects a hash change, or
  YouTube's auth flow refreshes its token). Server writes the new value
  back via the real `secrets_store.set_secret`.

- `image_used`:

  ```json
  { "type": "image_used", "job_id": "...", "row_idx": 0,
    "photo_id": "...", "source": "unsplash", "topic": "...",
    "used_on_date": "2026-05-22", "photographer": "...",
    "photo_url": "https://..." }
  ```

  Emitted by `agent/db_shim.record_image_use`. Server applies via the
  real `db.record_image_use` and appends to the credits file via
  `core.image_gatherer.append_credits_entry`.

- **Hello frame extension** (the existing pairing/auth hello, augmented):

  ```json
  { ...existing hello fields...,
    "pending_results": [
      { "job_id": "...", "row_idx": 0, "iso_date": "2026-05-22",
        "platform": "YouTube Video", "status": "success",
        "payload": { ...event payload... } }
    ]
  }
  ```

  Sent on every reconnect. Server idempotently applies (dedup key
  `(job_id, row_idx, platform)`), forwards to any attached browser, and acks
  on the socket; agent clears the buffer on ack.

## Data flow (hybrid happy path)

1. User opens the dashboard. The browser's `/agent/ws` socket already
   reports whether a paired agent is online.
2. User uploads spreadsheet + maps columns + picks dates and platforms.
   Server-side `ReviewEntry` objects exist for each date (LLM titles,
   schedules, descriptions all baked in — unchanged from today).
3. The dashboard chip shows `via agent: <device-name>`. User clicks Upload.
4. Browser POSTs `/upload?path=agent`. Flask calls `agent_dispatch.start(...)`.
5. Dispatcher applies `has_successful_upload` filter; pulls every needed
   credential (Playwright sessions + YouTube OAuth) from `secrets_store`;
   builds the envelope; sends `job_plan` to the chosen agent through the
   relay.
6. Agent's `dispatch.py` installs `secrets_shim`/`db_shim` as
   `core.secrets_store`/`core.db` in `sys.modules`, loads the envelope's
   `credentials` map into the shim's in-memory dict, resolves local paths
   from its scan map, calls `agent/run_batch.run(envelope, emit)`.
7. `run_batch` runs the parallel pool. Each row calls the existing
   uploader. Uploader calls into `core.secrets_store` are transparently
   served by the shim; `RemotePlaywrightSession` wraps the same shim for
   Playwright-style sessions. Bytes go straight to the platform.
8. Per-row events stream back through the relay → SSE queue → browser.
   Same event shapes as today.
9. As credentials change (Playwright session refresh, YouTube token
   refresh), `credentials_updated{key, value}` events flow back; server
   persists each via `secrets_store.set_secret`. Rock image-use is
   reported via `image_used` events; server applies through `db` +
   `image_gatherer`.
10. On `success` events: server's relay handler records `upload_history`
    via `db.record_upload`.
11. Job ends → `done` event → tempdir deleted on agent. Web-only path
    remains fully available; nothing about it changed.

## Error handling / edge cases

- **No agent online when chip is "agent":** dashboard greys the chip and
  defaults to web. If user explicitly chose agent and then the agent
  disappeared, the POST returns a clear error; user retries on web.
- **Agent disconnects mid-run:** `dispatch.py` keeps the in-flight pool
  running; events buffered. Reconnect within the bounded window replays the
  buffer. `pending_results` covers durability of completed rows for any
  reconnect, however late.
- **Agent process killed mid-row:** that row is lost. Idempotent-skip on
  re-run picks up only the rest.
- **VPS restart mid-run:** agent's socket dies; agent keeps running and
  reconciles via `pending_results` on next reconnect.
- **Browser closed:** server drops events; dashboard reads `upload_history`
  on reopen and re-subscribes to live events.
- **Multiple online agents:** server picks the most-recently-seen one. Chip
  shows the picked device's name so the user knows.
- **Expired platform session:** the row errors (per the deferred-local-login
  decision); user re-auths via web-only login UI; the next envelope picks up
  the refresh.
- **Missing local file for a selected date:** the agent's path-resolution
  step emits an `error` event for the row. Idempotent-skip behaves as today.
- **Protocol skew:** existing Phase 1 handshake refuses incompatible agents
  and prompts an update.

## Security

No new credentials. Reuses existing wss/TLS, device-token auth, and the
encrypted `secrets_store`. Session blobs travel over the same authenticated
control channel they would on the web-only path (just to a different
endpoint). Tempdir is per-job and cleaned on exit or at next agent start.

## Testing

- **Server unit (`tests/`):**
  - `agent_dispatch.start` builds the right envelope shape from given
    inputs; strips path fields; applies `has_successful_upload` filter
    correctly; bundles all required credentials from `secrets_store`.
  - Relay event ingestion routes to SSE queue with identical shapes.
  - `pending_results` ingestion is idempotent on repeat replays
    (dedup key `(job_id, row_idx, platform)`); ack roundtrip works.
  - `credentials_updated` ingestion writes back through `secrets_store`
    correctly (one test per key type — Playwright session + YouTube
    token).
  - `image_used` ingestion calls `db.record_image_use` + appends to
    credits.
- **Agent unit (`agent/tests/`):**
  - `run_batch` orchestration via stub uploaders: parallel pool size,
    circuit breaker tripping after threshold, email-waits-for-YouTube
    ordering.
  - `secrets_shim` get/set/delete/materialize semantics; mutation emits
    `credentials_updated`.
  - `db_shim.record_image_use` emits `image_used`; any other `db.*` call
    raises `NotImplementedError`.
  - `RemotePlaywrightSession` enter/exit/hash/emit lifecycle on top of
    the shim.
  - Event-buffer replay on stub disconnect.
- **Integration (`tests/integration/test_agent_end_to_end.py`, extended):**
  - Spin up the real relay + a stub-uploader agent. Dispatch a plan through
    the dashboard's path. Assert the browser receives the **same event
    stream** for the agent path as for the web-only path (this is the
    cross-path invariant — events must be indistinguishable to the
    dashboard).
- **Manual live verification:** dispatch one real upload through the live
  paired agent against `autoalert.pro`, on the same test workflow used to
  verify Phases 1–2b.

## Implementation phasing (for the writing-plans step)

This spec covers Phase 3. The writing-plans step will turn it into a
checkable plan. A likely cut, matching the small-PR rhythm of prior phases:

- **PR-A — server side.** `core/agent_dispatch.py` + envelope construction +
  `/upload` path flag + relay event ingestion → SSE queue + UI chip. Behind
  `HYBRID_AGENT_ENABLED`. Web-only path untouched.
- **PR-B — agent side.** `agent/dispatch.py` + `agent/run_batch.py` +
  `agent/secrets_shim.py` + `agent/db_shim.py` + `RemotePlaywrightSession`
  shim + scan-coverage extension. Live-verified end-to-end against
  autoalert.pro.
- **PR-C — disconnect/reconciliation.** Agent event buffer + `pending_results`
  in hello frame + server idempotent ingest + dedup logic. Plus any cleanup
  surfaced in PR-A/B.

A single combined PR is fine if it stays reviewable; three is the default.

## Out of scope (YAGNI for Phase 3)

- Local headed login on the agent for expired sessions (deferred to
  follow-up).
- Polished device-management UI (rename, revoke, multi-device picker —
  also deferred).
- `cancel_job` execution path (message type reserved, not implemented).
- Shared `core/orchestrator.py` extraction (do later if the two `run_batch`
  copies drift).
- Persisting `pending_results` across agent restarts (in-memory only for
  Phase 3 — if the agent process dies, the lost rows fall to idempotent-skip
  on the next run).
- Routing any media through the VPS on the hybrid path (was already out of
  scope per the parent spec).
