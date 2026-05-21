# Pre-Ship Checklist

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[-]` skipped (with reason)

## Top 3 (highest impact)
- [-] **P1.1** Headless SimpleCast + expired session — already handled in `core/playwright_session.py:_handle_login` (auto-relaunches headed). Audit was wrong.
- [x] **P1.2** Pre-flight port check (8080) + llamafile health warn at startup — `app.py` `_check_port_free`, plus `/health` endpoint
- [x] **P1.3** Tests for SimpleCast scheduling math — `tests/test_simplecast_schedule_math.py`, 29 cases. Refactored into `_compute_schedule_targets`, `_parse_picker_header`, `_compute_month_delta`.

## Logging
- [-] **L1** `app.py:82` — already logs at debug with exc_info; audit was inaccurate
- [x] **L2** `blueprints/upload.py:52-57` — promoted bare `except: pass` to `log.warning`
- [x] **L3** `simplecast_uploader.py:227` — logs original tz/value before falling back
- [x] **L4** `core/playwright_session.py` — `browser.close` failure now warning (real zombie risk); page/context stay debug
- [-] **L5** logs dir — already created at startup in `_configure_file_logging`

## Monitoring / Health
- [x] **M1** `/health` endpoint — probes db, llamafile, Chrome path; returns 503 if any failing
- [ ] **M2** UI banner when llamafile is down (deferred — `/health` covers on-call diagnosis; UI surface is lower-pain)
- [-] **M3** SSE milestone delivery on full queue — verified by reading `core/upload_jobs.py:170-176`: milestones use blocking `put`, lossy events use `put_nowait`. Already correct.

## Tests
- [x] **T1** SimpleCast scheduling math (covered by P1.3 — 29 cases including tz cross-day, hour mod-12, 5-min snap including the 58→00 wrap quirk, uppercase header parse, month delta across years)
- [ ] **T2** YouTube resumable upload retry on chunk failure (deferred — would need extensive mocking of googleapiclient; lower payoff than schedule-math tests)
- [ ] **T3** Session resume after crash (deferred)
- [ ] **T4** SimpleCast session expiry recovery (deferred — exists in `test_playwright_session.py` partially)
- [ ] **T5** Excel parser fallbacks (deferred)
- [ ] **T6** Playwright cleanup on exception (deferred)

## Documentation
- [x] **D1** `README.md` — operator-facing
- [x] **D2** First-run runbook — covered in README "First-run setup"
- [x] **D3** `state.db` schema reference — README "state.db schema" section
- [x] **D4** Recovery runbook — README "Recovery runbook" table
- [x] **D5** Env var reference table — README
- [x] **D6** OAuth rotation — README recovery table covers `token.json` and `simplecast_session.json` deletion paths

## On-Call Pain
- [ ] **O1** Partial-upload idempotency for SimpleCast retry (deferred — requires SimpleCast API/UI dedup logic; documented as "delete orphan draft" workaround in README would be a follow-up)
- [ ] **O2** DB busy-timeout fallback re-queue (deferred — 30s busy_timeout already in place per `core/db.py:28`; lossy edge case)
- [ ] **O3** `launch_mac.command` reaps llamafile on Flask exit (deferred — out of scope for Python-side; needs shell-script change)
- [x] **O4** USB-backup security note for `*_session.json` — README "Security model" section

## Summary

**Shipped:**
- `/health` endpoint at `/health` with db/llamafile/chrome checks
- Pre-flight port check (`FLASK_PORT`/8080) with clear stderr message
- Pre-flight llamafile warning (non-fatal)
- 29 unit tests for SimpleCast scheduling math (the most fragile uncovered code)
- Refactored helpers: `_compute_schedule_targets`, `_parse_picker_header`, `_compute_month_delta`
- Logging fixes: SSE terminal-event enqueue failures, tz fallback, browser.close zombie warning
- Operator-facing `README.md` with first-run setup, recovery runbook, env reference, schema, security model

**Test results:** 118 passed (existing) + 29 new = 147, no regressions.

**Deferred (lower-impact or out-of-scope):**
- T2-T6 — additional test coverage; followup PRs
- M2 — UI banner for llamafile down (CLI/`/health` is enough for now)
- O1 — SimpleCast idempotency (needs draft-dedup logic)
- O2 — DB re-queue (existing 30s busy_timeout is acceptable)
- O3 — shell-script change for llamafile reaping
