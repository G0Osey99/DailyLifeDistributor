# Multi-Tenant Phase δ — Concurrency + Agent Download

**Date:** 2026-05-23
**Status:** In progress
**Scope:** Lift web RunLock global → per-user, disk-budget admission, per-org platform soft-lock + YouTube quota, agent download landing pages + dashboard empty-state + settings download section, one-time pairing code embedded in download URL.

Builds on: phase α (schema/auth/admin), β (invites/roles), γ (2FA/audit). Implements PR-δ from the architecture spec at `docs/superpowers/specs/2026-05-23-multi-tenant-architecture-design.md`.

## Tasks

Each task is TDD: failing test → impl → passing test → exact commit message.

### Task 1 — Per-user web `RunLock`
- Add `class PerUserRunLock` in `core/media_session.py`: dict[user_id, holder run_id]. Same `acquire`/`release`/`holder` API but keyed by user_id.
- Two users → two simultaneous runs OK. Same user with active run → 409.
- Test: `tests/test_per_user_run_lock.py`
- Commit: `feat(media): per-user RunLock so two web users can upload concurrently`

### Task 2 — Wire per-user RunLock into `blueprints/media.py`
- Replace `_run_lock = ms.RunLock()` with `_run_lock = ms.PerUserRunLock()`.
- `run_init` reads `flask.session["user_id"]` and acquires per-user. 409 when *that* user's lock is held (not global).
- `_release_run` releases by (run_id) lookup over the per-user dict.
- Tests: extend `tests/test_media_chunk_upload.py` or new `tests/test_media_per_user_lock.py`.
- Commit: `feat(media): /media/run/init lock is per-user, not global`

### Task 3 — Disk-budget admission control
- Already partially in `run_init`: if `total_bytes` and `not has_free_space(total_bytes)` → 507. Add absolute floor: `disk_usage.free` < threshold (env `DLD_DISK_MIN_FREE_BYTES`, default 5 GiB) → 507 with message "VPS storage full; please use the agent path."
- Helper `has_minimum_free_space()` in `media_session.py`.
- Test: `tests/test_disk_admission.py`
- Commit: `feat(media): refuse new web runs when free disk < 5 GiB`

### Task 4 — `platform_locks` schema
- `core/db.py::init_db()`: add table
  ```sql
  CREATE TABLE IF NOT EXISTS platform_locks (
      org_id INTEGER NOT NULL,
      platform TEXT NOT NULL,
      locked_by_user_id INTEGER NOT NULL,
      locked_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      PRIMARY KEY (org_id, platform)
  )
  ```
- Idempotent. Test asserts the table exists after `init_db()`.
- Commit: `feat(db): platform_locks table (per-org platform mutex)`

### Task 5 — Per-org platform soft-lock helpers
- `core/platform_locks.py`: `try_acquire(org_id, platform, user_id, ttl_seconds=1800) -> bool`, `release(org_id, platform, user_id)`, `current_holder(org_id, platform) -> dict | None`. Expired locks (past `expires_at`) auto-release on next `try_acquire`.
- Test: `tests/test_platform_locks.py`
- Commit: `feat(platform-locks): acquire/release/auto-expire for per-org mutex`

### Task 6 — `yt_quota_usage` schema + helpers
- `core/db.py::init_db()`: add table
  ```sql
  CREATE TABLE IF NOT EXISTS yt_quota_usage (
      org_id INTEGER NOT NULL,
      quota_date TEXT NOT NULL,
      units_used INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (org_id, quota_date)
  )
  ```
- `core/quota.py`: `track_org_quota_usage(org_id, action, units=None)`, `get_org_quota_used(org_id) -> int`. Use same `_today_key()`. Daily cap from existing `DAILY_QUOTA` (10K).
- Test: `tests/test_org_quota.py`
- Commit: `feat(quota): per-org YouTube quota tracking (yt_quota_usage)`

### Task 7 — Agent download landing page `/download/agent`
- New blueprint `blueprints/download.py` (registered in `app.py`):
  - `GET /download/agent` — landing page; OS-detect from User-Agent (Windows / macOS / other); highlight matching button.
  - `GET /download/agent/windows` — 302 to current Windows binary `/agent/releases/<windows binary>` (looks up newest from manifest if available).
  - `GET /download/agent/macos` — same for macOS.
  - For now: stub the redirect destinations to `/agent/releases/dld-agent-windows.exe` and `/agent/releases/dld-agent-macos` (no-op if releases don't exist — 404 from existing route).
- Template `templates/download_agent.html` extends `base.html`.
- Auth: routes are session-gated through the global `before_request` hook just like other pages.
- Test: `tests/test_download_routes.py` — assert 200 on landing, 302 on each OS-specific.
- Commit: `feat(download): /download/agent landing + per-OS redirects`

### Task 8 — Empty-state download card on dashboard
- In `templates/index.html` (dashboard), render a download card block when the current user has zero **own** devices.
- Add to `blueprints/scan.py` (root route) a context var `show_agent_download_card` based on `core.devices.count_user_devices(user_id)` (we need that helper).
- `core/devices.py`: add `count_user_devices(user_id) -> int`.
- Test: `tests/test_dashboard_download_card.py` — login as a user with 0 devices, GET `/`, assert card markup present; user with 1 device, card absent.
- Commit: `feat(dashboard): empty-state download card when user has no devices`

### Task 9 — Persistent download section in `/settings/devices`
- Add a "Download the agent" block at the top of `templates/devices.html` (always visible, even when devices exist).
- Wire to `/download/agent` route.
- Test: extend `tests/test_devices_management_routes.py` or new `tests/test_settings_devices_download.py` — assert the block markup is in the response.
- Commit: `feat(settings): persistent download section in /settings/devices`

### Task 10 — One-time pairing code in download URL (30-min TTL)
- On `GET /download/agent` (when authed), call `devices.create_pairing_code(ttl_seconds=1800, user_id=...)` and render the code in the page (plus copy-button).
- 30-minute TTL (1800 s) — already supported by `create_pairing_code`.
- Test: `tests/test_download_pairing_code.py` — GET `/download/agent` returns 200 + a pairing-code string in the body that matches `create_pairing_code` format.
- Commit: `feat(download): one-time pairing code rendered on /download/agent`

### Task 11 — Agent-path no-lock invariant
- Verify (with a test) the agent-path branch in `blueprints/media.py::batch_run` does NOT acquire the per-user web lock. Each user's agent is on their own machine.
- Add `tests/test_agent_path_no_lock.py` — initialize a run (acquires web lock), call `/media/batch/run?path=agent`, assert the lock is **released** after dispatch (existing behavior, just asserted).
- Commit: `test(media): agent path releases per-user web lock after dispatch`

### Task 12 — Wiring concurrency model into `_dispatch_upload`
- Best-effort hook: `core/upload_jobs.py::_dispatch_upload` consults `platform_locks.try_acquire` before each platform upload (in the web-only path). If unavailable, emit `phase_change` event "Waiting for another user's upload" and re-poll up to 30s before failing the row. Release after the row completes.
- This is intentionally a soft-lock; the row error message names the lock holder (user id, no PII).
- Test: `tests/test_platform_lock_dispatch.py` — two users posting YT for same org → second sees `phase_change` blocked, then succeeds when first releases.
- Commit: `feat(platform-locks): web dispatch waits on per-org platform mutex`

## Notes on scope

- Phase δ is the last PR before deployment. Tests pass on this branch; review + PR follow in task #8.
- The `/download/agent` URL family is the user-facing stable API; underlying binary file names can move under `/agent/releases/` without breaking installer documentation.
- Per-org platform mutex is a soft-lock for UX. It does not replace platform-side rate limits.
- Agent-path uploads have no lock — each user's agent runs on their own machine.
