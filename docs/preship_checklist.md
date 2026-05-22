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
- [x] **M4** Container healthcheck — `deploy/docker-compose.yml` polls `/health` (with the `autoalert.pro` Host header) so `docker ps` shows healthy/unhealthy. Informational only: compose does not auto-restart on unhealthy, so an LLM blip flagging 503 won't bounce the app.

## Resilience
- [x] **R1** Per-platform circuit breakers — `core/circuit_breaker.py` + `_dispatch_upload`. A platform that hits repeated infra failures (broken session, network, unresponsive page) within a run is skipped for the rest of it instead of relaunching Chrome and burning the login timeout per date. The same breaker guards the LLM title call. Tests: `tests/test_circuit_breaker.py`, `tests/test_dispatch_circuit_breaker.py`.
- [x] **R2** Env-var validation hardening — integer coercion for the `*_LOGIN_TIMEOUT` / `MAX_CONTENT_LENGTH_BYTES` knobs and a HOSTED-mode `FLASK_SECRET_KEY` requirement so a public deploy can't silently run on an ephemeral key. `core/env_validation.py`, `tests/test_env_validation.py`.
- [x] **R3** Dependency CVEs — `requests >= 2.32.2` (CVE-2024-35195), `cryptography >= 43.0.1` (CVE-2024-6119).

## Tests
- [x] **T1** SimpleCast scheduling math (covered by P1.3 — 29 cases including tz cross-day, hour mod-12, 5-min snap including the 58→00 wrap quirk, uppercase header parse, month delta across years)
- [x] **T2** YouTube resumable upload retry on chunk failure — `tests/test_youtube_retry.py` covers the retryable-status classifier and the retry loop (retry-then-succeed on 5xx/network, raise on 4xx, exhaust retries) with a fake request object, so no live API calls are needed.
- [x] **T7** `/health` failure semantics — `tests/test_health_endpoint.py` asserts the db/llamafile/chrome check structure plus 503 with the failing subsystem flagged.
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
