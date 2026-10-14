# Daily Life Distributor Rebuild — Build Guide

Written 2026-10-14. Builds the desktop app described in `docs/rebuild/plan.md` (a copy of the Claude Docs draft "Daily Life Distributor — Rebuild Plan") in the order of its roadmap. Target: a new TypeScript project in `desktop/` inside this repo. The Python app beside it keeps running until cutover.

**The goal:** each day's devotional goes from three inputs (the vertical video, the email thumbnail, its spreadsheet row) to six destinations (YouTube Short, Spotify, Wistia, Vista Social for Instagram + Facebook, Rock Daily Experience, Rock Email/SMS) from one Mac app, with one approval per day and a browser only where no API exists.

**How it works:**

- **Phase 0 is yours:** commit these docs (M0), then get the keys, ids, test targets and a recording that only you can get (Part 4). It has no prompts.
- **Phases 1–3 are steps.** Each step is one `/goal` prompt pasted into a fresh Claude Code session (≤ 4000 characters, checked by `scripts/check_prompts.py`).
- **Each session** builds on a branch, writes a build log, updates `docs/rebuild/build-tracker.md`, and stops with its PR open and CI green. You run **Your check**, a few minutes of hands-on verification, then the PR is merged.
- **After cutover:** that work is listed at the end of Part 3 without prompts. Write them when you schedule them.

| Part | Contents |
|---|---|
| [1](#part-1--how-to-run-this-guide) | How to run it: the loop, the step template, working rules, log format, phase overview |
| [2](#part-2--locked-decisions-and-shared-services) | Locked decisions (D1–D12) and shared services (§S1–§S14) every step builds on |
| [3](#part-3--steps) | The steps, Phase 1 → Phase 3, then Later |
| [4](#part-4--manual-items) | Manual items (M0–M14): everything only you can do |
| [5](#part-5--reference) | Reference: what the legacy code does (checked 2026-10-14), platform facts to confirm, secret names |
| [6](#part-6--coverage-matrix) | Coverage matrix: every plan element → the step that builds it |

---

## Part 1 — How to run this guide

### 1.1 The loop

For each step, in order:

1. Do the manual items listed in the step's **Needs** (Part 4).
2. Open a fresh Claude Code session at the repo root, on an up-to-date `main`.
3. Paste the step's prompt exactly.
4. When the session reports done, read `docs/rebuild/build-log/step-X.Y.md` and do its *Manual items*.
5. Run **Your check**. If it fails, paste what you saw into the same session and let it fix it on the same branch.
6. When it passes, tell the session "merge" (or squash-merge the PR yourself). Next step.

Editing a prompt? Keep it ≤ 4000 characters, starting with `/goal Step X.Y`, naming its build log, and without the banned keyword the checker knows about. Run `python3 scripts/check_prompts.py` to confirm.

### 1.2 The step template

Every step in Part 3 is a card with the same shape:

```
#### Step X.Y — Name
Builds: plan sections / services · Needs: steps + manual items · Unlocks: what you can do afterwards

<prompt>

Your check: what you verify by hand
```

Every prompt has the same five sections, so sessions read them the same way:

```
/goal Step X.Y — <name>. Read docs/rebuild/build-guide.md <parts>, docs/rebuild/plan.md <sections>, <legacy files>, <previous build log>.

BUILD  numbered deliverables: files, functions, commands, behaviour
TEST   what Vitest must prove (offline)
CHECK  the run the session does itself on this Mac, and what it records
DONE   Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-X.Y.md; PR "step-X.Y: ...".
```

To add a step later, copy both blocks, give it the next free number, add a row to `docs/rebuild/build-tracker.md` and a line to Part 6, and run the checker.

### 1.3 Working rules (every prompt enforces these)

1. **The plan is the spec; the legacy code is the reference.**
   - `docs/rebuild/plan.md` decides *behaviour*. This guide decides *build order and interfaces* (Part 2). If they conflict, follow the plan and log the conflict under *Decisions made*.
   - For rules the plan keeps (filename dates, Rock text, YouTube description, breaker, quota, waits), port what the legacy Python *does* (Part 5 §A has file:line) and its test cases, not its structure.
   - CLAUDE.md and README.md are out of date in places (Part 5 §A15). The code wins.
2. **Live sources beat this guide.** The plan's platform facts (Part 5 §B) came from search summaries, not the pages themselves. Before coding against an API, confirm it in the official docs and with a read-only call using the real key (`dld probe <service>`, from 1.4). If they disagree, the live source wins: note it in the log and update Part 5.
3. **Stack and licenses (D3).** TypeScript `strict`, ESM, npm, Biome (lint + format), Vitest. Dependencies must be permissive (MIT, BSD, Apache-2.0, ISC); no GPL/AGPL packages or bundled binaries, no copied code without a license. Pin exact versions.
4. **Use the shared services (Part 2.2).** Never re-implement one. If a service lacks something, extend it in place, with tests.
5. **Keep earlier commands working.** Every `dld` command from an earlier step keeps its flags and output. Add a regression test whenever you touch one.
6. **Tests with every change.**
   - Unit tests are offline and fast: HTTP faked with undici `MockAgent`, files in temp dirs, a pinned clock.
   - Browser code is tested against small hand-written HTML pages that hold only the roles and labels the selectors use. Never save a real logged-in page.
   - Anything that touches a real platform is an integration test that runs only with `DLD_INTEGRATION=1`.
7. **Nothing real in git.** The repo is public.
   - Never commit videos, thumbnails, real spreadsheets, browser profiles, saved web pages, tokens or keys.
   - Secrets live only in the macOS keychain (§S4); logs redact them (§S5). CI runs a secret scan (1.1).
   - Fixtures are synthetic (ffmpeg `lavfi` media, hand-written rows, filename lists) or recorded API responses with every id, name, email and token replaced.
8. **Nothing public, nothing irreversible.**
   - A session runs `plan` or `drafts` mode only (D5).
   - `live` mode, spending money, changing account or platform settings, publishing a release, touching the old app, its server or its data, and deleting anything a person made are Manual items.
   - A session may remove only items with `origin: created` from a drafts-mode task (§S7). Never an item `dld` merely found.
9. **Gates before done:**
   - `npm --prefix desktop run check` (biome, `tsc --noEmit`, vitest) green;
   - `npm --prefix desktop run dld -- doctor` shows no new failures (from step 1.1 on);
   - `python3 scripts/check_prompts.py` passes;
   - build log written in format 1.4; `docs/rebuild/build-tracker.md` row updated (status, PR, log link);
   - work on branch `rebuild/X.Y-<slug>`, committed and pushed with a PR to `main` and CI green (CLAUDE.md "Commit / push / merge" steps 2–4). Then stop. Merge (step 5, squash) only when Ryker says his check passed. Never step 7 (the deploy).
10. **Decide, don't ask.** Make the call and record it with a reason under *Decisions made*. Stop only for a Part 4 item.
11. **Configuration, not code** (plan "Architecture").
    - Everything specific to LCBC or the Daily Life series lives in the series config (§S2), never in code: folders, column names, text, footers, times, Rock ids and attribute keys, Wistia projects, profile names.
    - A new destination is a connector plus config; a second series is config only.

### 1.4 Build log format

`docs/rebuild/build-log/step-X.Y.md`:

```
# Step X.Y — <name>
Date · PR · plan sections covered
## Done            what was built (file:function) and the test that covers it
## Decisions made  one line each, with the reason
## Gates           check summary, doctor output, CHECK results (timings, links, numbers)
## Manual items    what Ryker must do next, exactly
## Known gaps      anything deferred, and the step that owns it
## Next step needs facts the next session shouldn't have to rediscover (APIs, ids, gotchas)
```

### 1.5 Phase overview

| Phase | Steps | Outcome | Gate (plan "Roadmap") |
|---|---|---|---|
| **0 — Unblock** | M0–M10 | These docs committed; keys, ids, test targets and the Spotify recording in hand | **G0** every key and test result in hand (`dld probe` all green once 1.9 is built) |
| **1 — Engine** | 1.1–1.10 | `dld ship <date> --mode drafts` ships one day to YouTube, Wistia, Vista Social and both Rock flows | **G1** one real day shipped as drafts (1.10) |
| **2 — App** | 2.1–2.6 | The desktop app: week board, approvals, preview, preflight, Ship, logins, Spotify | **G2** a full week as drafts (2.6) |
| **3 — Cutover** | 3.1–3.4 | Verify, Pull a day, signed auto-updating build, two weeks beside the old app, the switch, legacy retired | **G3** two clean weeks of drafts, then the switch to live and the old server off |
| **Later** | — | Edit everywhere, template editor, write-back, summaries, station mode, a second series, other churches | — |

**Why this order** (plan "Roadmap"):

- The engine comes before the app: a command line that ships one real day as drafts proves every integration before any UI exists.
- Phase 0 is mostly waiting on other people, so start it today. Steps 1.1–1.4 need no keys.

**Parallelism:** steps are sequential, with two exceptions. Step 1.7 (Vista Social) can run beside 1.8–1.9 (Rock). Steps 2.4–2.5 (browser, Spotify) can run beside 2.2–2.3. If two sessions overlap, merge one before starting the other.

---

## Part 2 — Locked decisions and shared services

### 2.1 Decisions (prompts cite these as D1–D12)

- **D1 Scope** (plan "Platform by platform"). Six destinations, one series ("Daily Life") at first.

  | Destination | Integration | Needs | Produces |
  |---|---|---|---|
  | YouTube Short | Data API v3 | video, approved title, description, tags | `youtube.videoId`, `youtube.url` |
  | Wistia | Upload API | video | `wistia.hashedId`, `wistia.name` (`app YYMMDD`) |
  | Vista Social (IG + FB) | Vista Social API (add-on, M3), else browser (2.4) | video, caption | `vista.postId` |
  | Spotify | browser automation of Spotify for Creators | video, episode title, description | `spotify.episodeUrl` |
  | Rock Daily Experience | Rock REST | row (passage, scripture, prayer, episode title), stock photo, the Wistia media once Rock has synced it | `rock.parentId` + three child ids |
  | Rock Email / SMS | Rock REST | description, email thumbnail, `youtube.url` | `rock.emailItemId` |

  Dropped: the horizontal YouTube video, YouTube custom thumbnails, SimpleCast. The plan's "keep the browser path as a fallback" for Rock is built only if M1 brings no REST key (its control ids are hard-coded and break with Rock updates); with a key, a Rock failure goes to assisted mode (2.4).
- **D2 One Mac, one person** (plan Q&A, 2026-10-14). macOS only, one universal build for Apple Silicon and Intel. All state lives on that Mac: no sync, accounts or roles.
- **D3 Stack** (plan "Tech stack"): Electron + TypeScript end to end.
  - The engine is a plain TypeScript library (`desktop/src/engine/`). The CLI `dld` uses it in-process; from 2.1 the app runs it in an Electron utility process.
  - UI: React + Vite through electron-vite. The plan leaves React or Svelte open; React, for its ecosystem.
  - Libraries:
    - commander (CLI), zod (config), exceljs (spreadsheet);
    - SQLite through `node:sqlite` or better-sqlite3, decided once in 1.2 so the CLI, the tests and Electron share one runtime;
    - google-auth-library (YouTube OAuth); YouTube, Wistia, Rock and Vista Social calls through §S6 over `fetch`, including YouTube's resumable upload protocol, which the Node googleapis client doesn't implement;
    - mp4box.js (BSD-3) for media checks: pure JavaScript, nothing to bundle. ffprobe is used only if installed, for containers mp4box can't read. The plan says "a bundled ffprobe": common Mac builds are GPL and a binary complicates the universal build, so this is a recorded deviation.
    - playwright-core (browser), `@anthropic-ai/sdk` (titles), electron-builder + electron-updater (3.2).
  - To stay in Python instead (the plan's third option), change D3 and §S1 before 1.1. Everything else in this guide holds.
- **D4 Integrations.**
  - Official APIs for YouTube, Wistia, Rock and Vista Social.
  - Browser automation only for Spotify for Creators (no upload API), and for Vista Social if the API add-on is declined (M3).
  - Never private or reverse-engineered APIs.
  - Browser automation drives the installed Google Chrome with one persistent profile per platform; the person logs in on their own screen.
- **D5 Ship modes.** Every command that posts takes `--mode plan|drafts|live`; `plan` is the CLI default.
  - `plan` prints every request it would send (redacted) and sends nothing.
  - `drafts` uses real platforms with nothing public and nothing sent:
    - YouTube: `private` with no `publishAt`.
    - Wistia: the test project (M4).
    - Vista Social: a draft, if Vista has one (M3).
    - Spotify: saved as a draft.
    - Rock: M2's staging server or test channels, never the live channels. A Rock email item in a live channel may be sent by Rock's own job (M1).
    - A destination with no safe draft target runs in `plan` form during a drafts run and says so.
  - `live` schedules public posts.
  - Drafts targets must differ from live targets wherever a platform has both. `dld series check` refuses a series where they match, and the Rock and Wistia connectors refuse a drafts-mode write to a live channel or project.
  - Each task records the mode that created it; drafts and live never mix for one destination on one day. The app's Ship button uses `drafts` until the switch in 3.3.
- **D6 Approval** (plan "Workflow and UX"). A day ships only with an approved Shorts title: an explicit Approved state. Today the first suggestion auto-fills and starting the upload counts as approval. There is no auto-approve setting.
- **D7 Time.**
  - Publish times are configured and shown in America/New_York and stored as UTC.
  - Scheduling happens on the platforms (`publishAt` and equivalents), so the Mac stays awake only while it uploads (`caffeinate` / `powerSaveBlocker` during a run).
  - The YouTube quota day rolls over in America/Los_Angeles (legacy, Part 5 §A12).
- **D8 Secrets in the macOS keychain** (plan "Tech stack").
  - The plan names Electron's `safeStorage`, which works only inside a running Electron app. The CLI needs the same secrets, so both read keychain items directly (§S4); same intent, one mechanism.
  - Nothing secret goes in config files, the database or logs.
- **D9 YouTube.**
  - Reuse the Google Cloud project the legacy app uses (M5). Uploads from an API project that hasn't passed Google's audit and was created after July 2020 are locked private.
  - Add a "Desktop app" OAuth client in that same project. Keep its consent screen "In production": in "Testing", refresh tokens expire after 7 days. Scope `youtube`, as today.
  - Upload cost and daily cap come from config (100 units and 10,000 today; plan Risks). The ledger refuses an upload that would pass the day's cap and says which day it fits.
- **D10 Order from declarations** (plan "Architecture").
  - Connectors declare `needs` and `produces`; the runner derives the order and the waits from them.
  - Two waits today:
    - Rock Email needs `youtube.url` (legacy cap 2 h).
    - Rock Daily Experience needs the Wistia media and Rock's sync of it.
  - A waiting task never blocks the others. It stays `waiting` across runs: the next `dld ship`, or the app's timer (3.1), checks it again until its need exists or its cap passes. Rock's sync may run only hourly or daily, so uploading days ahead is the normal case (plan Risks).
- **D11 The platform is the truth** (plan "Keep, drop, add").
  - Before posting, a connector asks the platform whether this day is already there (`find`). A deleted post re-ships, and a manual post is not duplicated.
  - A found item is recorded with `origin: found` and is never removed or unscheduled by `dld` without a person confirming it (3.1).
  - In drafts mode, `find` looks only at drafts targets and at items `dld` created in drafts mode; it never matches a live item.
  - The local history is a cache.
- **D12 The legacy app stays live until the switch.**
  - Nothing in `desktop/` imports or calls the Python app.
  - Sessions don't edit the Python code, its CI workflows, `deploy/` or the server, except step 3.4 after the switch.
  - Reading legacy files is fine, and so is reading a copy of its database in 3.3.

### 2.2 Shared services (build once, then always reuse)

**§S1 Layout and paths** (step 1.1).

```
desktop/
  package.json tsconfig.json biome.json vitest.config.ts
  src/engine/    the library: paths, log, config, db, secrets, http, planner/, runner, breaker, quota,
                 connectors/, rock/, titles, templates, photos, media, browser/, api
  src/cli/       dld (commander)
  src/main/ src/preload/ src/renderer/     Electron (2.1)
  prompts/       LLM prompts (titles, photo keywords)
  examples/daily-life.series.json          the seed series, no secrets
  test/ test/fixtures/ test/fakes/
```

- Data dir: `~/Library/Application Support/Daily Life Distributor/`. `DLD_DATA_DIR` overrides it, and tests always use a temp dir. It holds:
  - `dld.db`, `profiles/<platform>/`, `logs/`, `evidence/<task id>/`, `cache/`;
  - `credits/credits-YYYY-MM.tsv`.
- `npm --prefix desktop run dld -- <command>` runs the CLI from source; `npm --prefix desktop run check` runs biome + tsc + vitest.

**§S2 Series config** (step 1.2): `src/engine/config.ts`, zod. Unknown keys are rejected, and errors name the field path.

- `Series` fields:
  - `id`, `name`, `timezone`;
  - `media`: folder, video and thumbnail extensions;
  - `sheet`: path to the OneDrive-synced .xlsx, sheet name, `columns` (the legacy mapping keys → header, Part 5 §A2);
  - `titles`: model, count (5), optional local `endpoint`;
  - `templates`: per destination title / description / caption / footer (§S9);
  - `times`: per destination, HH:MM New York;
  - `destinations`: `enabled` plus per-connector settings (Wistia project, Vista profiles, Rock base URL, channel ids, attribute keys, Spotify show URL);
  - `waits`: per need, poll seconds and cap;
  - `limits`: Shorts max 180 s, aspect tolerance, YouTube quota costs and cap, breaker thresholds;
  - `drafts`: the drafts targets per destination (D5): Wistia test project, Rock server or test channels, or "plan" when there is no safe target.
- Stored in the database; `dld series import|export|show|check`. `check` enforces D5's "drafts targets differ from live targets".
- `examples/daily-life.series.json` holds today's values from Part 5 §A, with placeholders for ids not yet known.

**§S3 Store and task states** (step 1.2): `src/engine/db/`. SQLite (driver per D3) with WAL and foreign keys; migrations `NNN_name.sql` applied in order.

- Tables:
  - `series`;
  - `days`: series, date, files, row JSON, suggestions, approved title + time + source;
  - `tasks`: day × destination: state, mode, attempts, `produced` JSON (each artifact with its value and `origin: created | found`), error, timestamps;
  - `events`: every transition (entity, from, to, reason, data, at);
  - `quota`: service, quota day, units;
  - `photos`: image history for the no-repeat windows;
  - `settings`;
  - `locks`: holder pid, host, heartbeat.
- Task states (plan "Architecture"):
  - `waiting → ready → running → scheduled → verified`;
  - `running → failed | needs_login | waiting`;
  - `failed → ready`, `needs_login → ready`;
  - `scheduled | verified → running` (re-ship, edit);
  - any → `pulled`.
- `transition(db, taskId, to, {reason, data})` writes the state and an `events` row in one transaction. An illegal move throws `IllegalTransition`.
- Crash recovery: when the engine starts, every `running` task whose lock holder is gone moves to `failed` ("interrupted"), then to `ready`. A lock whose heartbeat is older than 2 minutes is broken.

**§S4 Secrets** (step 1.2): `src/engine/secrets.ts`.

- `get/set/delete/has(name)` on macOS keychain generic passwords (service `daily-life-distributor`, account = the key name) through `/usr/bin/security`.
- Writes go through `security -i` on stdin, so a value never appears in a process list. Values are written hex-encoded (`-X`), so JSON and quotes survive `security -i`'s tokenizer; reads decode them.
- `dld secrets set <name> [--from-file <path>]` reads a hidden prompt or a file. `dld secrets list` prints names with present / missing only. The names are in Part 5 §C.
- Every value read is registered with the redactor (§S5). Tests use an in-memory backend.

**§S5 Logs and evidence** (steps 1.1, 2.4).

- One JSON-lines log per run in `logs/`.
- `redact()` removes every registered secret value, the `Authorization`, `x-api-key` and `Authorization-Token` headers, and OAuth tokens before anything is written or printed.
- A failed browser task keeps a screenshot, a DOM snapshot and the URL in `evidence/<task id>/` (2.4). Evidence stays in the data dir, never in the repo.

**§S6 HTTP** (step 1.4): `src/engine/http.ts`. One client per service over `fetch`, with:

- base URL, and the auth header from §S4;
- timeout;
- retries with backoff and jitter on network errors, 408, 429 and 5xx, honouring `Retry-After`;
- file bodies streamed from disk (`fs.openAsBlob`, or a stream with `duplex: 'half'`), never read whole into memory, with byte progress;
- redacted logging;
- the `plan`-mode recorder (records the request, returns a canned response);
- fault injection for tests and drills: `DLD_FAULT=<service>:<kind>@<when>`, e.g. `youtube:network@50%`.

Tests use undici `MockAgent`.

**§S7 Connectors and the runner** (step 1.4): `src/engine/runner.ts`, `src/engine/connectors/`.

```ts
interface Connector {
  id: Destination
  needs: Artifact[]                              // e.g. 'video', 'title.approved', 'youtube.url'
  produces: Artifact[]
  preflight(day, ctx): Promise<Issue[]>          // each issue carries its fix
  find(day, ctx): Promise<Produced | null>       // already there? (D11) → origin 'found'
  post(day, ctx): Promise<Produced>              // honours ctx.mode (D5) → origin 'created'
  verify(day, produced, ctx): Promise<Check[]>   // title, time, visibility read back
  unschedule?(day, produced, ctx): Promise<void> // live: back to private / draft / inactive
  remove?(day, produced, ctx): Promise<void>     // drafts only, origin 'created' only
  probe(ctx): Promise<ProbeResult>               // read-only access check
}
```

- `ctx` carries the mode, series, secrets, http, log, clock, a cancel signal, and the day's artifacts so far.
- The runner:
  - builds the graph from needs and produces for the selected days × enabled destinations;
  - runs ready tasks, two uploads at a time;
  - parks a task in `waiting` until its needs exist (D10);
  - retries with backoff, and keeps one circuit breaker per connector (legacy thresholds, Part 5 §A9);
  - emits progress events that the CLI prints and the UI lanes show.
- `remove` refuses anything but a drafts-mode task's `origin: created` artifacts, whatever the caller asks.
- A registry maps destination ids to connectors. A fake connector set lives in `test/fakes/`; `DLD_FAKE_CONNECTORS=1` makes the CLI use it.

**§S8 Planner** (step 1.3): `src/engine/planner/`.

- `parseNames`: filename dates, ported from legacy, with explicit day-of-month validation (a JavaScript `Date` turns Feb 30 into Mar 2 instead of failing).
- `readSheet`: exceljs, with the legacy header and date rules. exceljs returns date cells as UTC midnight, so read them with UTC getters; custom-format dates may come back as serial numbers.
- A OneDrive file that hasn't been downloaded (allocated blocks 0, size > 0), or that is mid-sync (a corrupt zip), is "not ready yet": retry after a pause, then report it as a gap.
- `scanMedia`, and `probeMedia` (§S10).
- `planDays(series, from, days)` returns each day's files, row, title state, task states and gaps, e.g. "Thu Oct 23 has no video and publishes in 2 days".

**§S9 Templates** (step 1.5): `src/engine/templates.ts`.

- Per-destination text comes from series templates with `{{field}}` placeholders and a few named helpers (`monthDayShort`, `monthDayLong`, `longDate`, `normalizeVerse`, `shortsDescription`, `withFooter`).
- Every legacy text rule (Part 5 §A3, §A5–§A7) is a template plus a helper, with the legacy cases as golden tests.

**§S10 Media probe** (step 1.3). mp4box.js reads .mp4 / .mov headers (the `moov` box may sit at the end of the file): width, height, rotation, duration, codecs. Other containers use ffprobe if it's installed, else the probe reports "can't check this file". `isVertical` respects rotation.

**§S11 Browser runner** (step 2.4): `src/engine/browser/`.

- playwright-core with `launchPersistentContext(profiles/<platform>, { channel: 'chrome', headless: false })`.
- Login: Google and some SSO pages refuse sign-in in a browser under automation. `connect(platform)` therefore opens plain Chrome (no automation) on the same profile directory for the person to sign in; once that window closes, Playwright uses the profile.
- `checkLogin(platform)`.
- `withPage(platform, fn)`: saves evidence on failure (§S5).
- `assist(platform, prefill)`: assisted mode. It opens the form filled in and waits for the person to publish.

**§S12 Titles** (step 1.5): `src/engine/titles.ts`.

- `suggestTitles(day)` returns N titles from the transcript column, using the legacy prompt rules (Part 5 §A8). The transcript is cut to 2000 characters when the prompt is built; the cache key hashes the full text.
- The model is Claude through `@anthropic-ai/sdk` with structured output (model from the series, default `claude-opus-5-5`). If the series sets `titles.endpoint`, a local OpenAI-compatible server is used instead (offline).
- `approve(day, title)` and `unapprove(day)` set the D6 state.

**§S13 Stock photos** (step 1.9): `src/engine/photos.ts`. Ports the legacy behaviour (Part 5 §A10):

- 3 nature keywords from the verse via §S12's model;
- Unsplash then Pexels, portrait;
- no photo reused within 60 days, no topic within 14;
- credits appended to `credits/credits-YYYY-MM.tsv`;
- `swap(day)` takes the next candidate.

**§S14 Engine API** (step 2.1). One typed facade: `plan`, `titles`, `approve`, `preview`, `preflight`, `ship`, `status`, `verify`, `pull`, `connections`, `series`, `subscribe(events)`.

- The CLI calls it in-process; the UI calls it over a MessagePort to the engine's utility process.
- The UI never touches the database, the keychain or the network directly.
- One writer at a time: the `locks` row (§S3, with heartbeat) stops the app and a CLI ship from running together.

---

## Part 3 — Steps

### Phase 0 — Unblock (manual)

No prompts. Do M0 first: it puts these docs on `main`, which every session reads. Then do M1–M10 in Part 4 and record every non-secret answer in `docs/rebuild/access.md`. Start M1–M4 and M8 today: they wait on other people or on a real upload day. Steps 1.1–1.4 can run meanwhile.

### Phase 1 — Engine

#### Step 1.1 — Desktop scaffold, `dld doctor`, CI
Builds: §S1, §S5 (logs) · Needs: M0, M9 · Unlocks: `dld --version`, `dld doctor`, the desktop CI workflow

```text
/goal Step 1.1 — desktop scaffold, `dld doctor` and CI. Read docs/rebuild/build-guide.md Parts 1 and 2 (D2, D3, D8, D12, §S1, §S5) and Part 5 §B (hosts), docs/rebuild/plan.md ("Tech stack", "Architecture") and the "Commit / push / merge" section of CLAUDE.md.

BUILD
1. desktop/ as §S1: package.json (private, "type": "module", engines = the current Node LTS; scripts dld (tsx src/cli/index.ts), check (biome check . && tsc --noEmit && vitest run), test), tsconfig (strict, NodeNext), biome.json, vitest.config.ts. Exact versions. Deps now: commander, zod. Dev: typescript, tsx, vitest, @biomejs/biome, @types/node.
2. src/engine/paths.ts: dataDir() (DLD_DATA_DIR, else ~/Library/Application Support/Daily Life Distributor) with subdirs made on demand. src/engine/log.ts (§S5): a JSON-lines run logger and redact(value), which strips registered secret values and the Authorization, x-api-key and Authorization-Token headers from strings and nested objects.
3. `dld --version` and `dld doctor`: a table of ok / warn / fail with a one-line fix each; exit 1 on any fail. Rows: macOS (fail elsewhere, D2); Node version; data dir writable, free space (warn < 10 GB); Google Chrome installed (warn until 2.4); ffprobe (optional, D3; info only); /usr/bin/security present; each service host in Part 5 §B reachable (warn). Each row is a small probe function so later steps add rows.
4. .gitignore: add desktop/node_modules, desktop/out, desktop/dist, desktop/release; keep every existing entry.
5. .github/workflows/desktop.yml: on pull_request and push to main with paths desktop/**, docs/rebuild/**, scripts/check_prompts.py; runs-on macos-latest; setup-node with npm cache; npm ci and npm run check in desktop/; python3 scripts/check_prompts.py; a secret scan of the diff (gitleaks or an equivalent MIT/Apache tool, rule 7). Leave ci.yml and release-agent.yml untouched (D12).
6. Root CLAUDE.md: insert one short block right under the title: a rebuild is in progress; new work goes in desktop/ and follows docs/rebuild/build-guide.md; the rest of this file describes the legacy Python app, which keeps running until cutover (D12); where this file and the legacy code disagree, guide Part 5 §A has the checked facts. Change nothing else.
7. docs/rebuild/build-tracker.md: fill step 1.1's row only.

TEST: redact() on nested objects, headers and token strings; doctor with every probe mocked: all ok exits 0, a missing security binary fails and exits 1; dataDir honours DLD_DATA_DIR.
CHECK: run `npm --prefix desktop run dld -- doctor` on this Mac and paste the table into the log. Every fail becomes a Manual item naming the Part 4 item that fixes it. Push the branch, open the PR and confirm both CI workflows pass.
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-1.1.md; PR "step-1.1: ...".
```

**Your check:** `npm --prefix desktop run dld -- doctor` shows no red rows (warnings for things you haven't set up yet are fine). On GitHub, the PR shows the new `desktop` workflow and the legacy `CI` workflow both green.

#### Step 1.2 — Series config, store, task states, secrets
Builds: §S2, §S3, §S4 · Needs: 1.1 · Unlocks: `dld series`, `dld secrets`, `dld db`, a migrated database

```text
/goal Step 1.2 — series config, SQLite store, task state machine and keychain secrets. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D3, D5, D6, D8, D10, D11, §S2, §S3, §S4), Part 5 §A (legacy values) and §C (secret names), docs/rebuild/plan.md ("Architecture", "Keep, drop, add") and docs/rebuild/build-log/step-1.1.md.

BUILD
1. The SQLite driver, once for every runtime (D3): node:sqlite if both the Node LTS and the Electron version you would pin for 2.1 expose it without a flag (check by running it under `ELECTRON_RUN_AS_NODE=1` with electron as a dev dependency); otherwise better-sqlite3, with the CLI and Vitest run under Electron's Node so there is one native ABI. Record the choice and the evidence.
2. §S2 src/engine/config.ts: zod schemas for Series and its parts, strict (an unknown key fails naming its path). sheet.columns uses the legacy mapping keys (Part 5 §A2); youtube_title and podcast_title are optional (the plan retires them if unused). templates are strings (filled in 1.5). waits {pollS, capS} per need. drafts targets per destination, with "plan" allowed; a check that drafts targets differ from live targets (D5).
3. desktop/examples/daily-life.series.json: today's values from Part 5 §A (publish times, footers, Rock base URL and channel GUIDs and ids, Wistia folder "Daily Life App" and label "app YYMMDD", Vista profiles Facebook + Instagram, YouTube category 22, quota costs and cap 10000, email wait cap 7200 s, breaker 3 failures / 120 s); "TODO" for unknowns (Wistia project ids, drafts targets, Spotify show URL and time, Rock attribute keys). It must validate.
4. §S3 src/engine/db/: open(path) (WAL, foreign keys, busy timeout 5000 ms); migrate() with migrations/001_init.sql (tables per §S3; unique (series_id, date) on days and (day_id, destination) on tasks; index tasks(state)); small repo helpers, no ORM; transition() exactly as §S3, with IllegalTransition; the locks table with pid, host and heartbeat, and crash recovery at open (§S3). Timestamps UTC ISO-8601.
5. §S4 src/engine/secrets.ts: KeychainSecrets over /usr/bin/security (add and delete through `security -i` on stdin; values hex-encoded with -X; reads decoded), MemorySecrets for tests, one interface; every value read is registered with redact().
6. CLI: `dld series import <file> | export [id] [file] | show [id] | check <file>`; `dld secrets set <name> [--from-file <path>] | list | delete <name>` (hidden prompt; list = Part 5 §C names with present / missing, never values); `dld db migrate | status | backup <path>`. doctor gains one row per secret (warn when missing).

TEST: the example round-trips import → export unchanged; a misspelled key fails naming its path; drafts == live fails check; migrate is idempotent; every allowed transition passes and at least four illegal ones throw; transition + event are atomic (inject a failure between them); crash recovery moves an orphaned running task to ready via failed; a stale lock is broken; KeychainSecrets builds the right commands (spawn mocked), never puts a value in argv, and round-trips JSON with quotes.
CHECK: on this Mac: `dld series import examples/daily-life.series.json`, `dld series show`, `dld db status`; set, read back and delete a dummy JSON secret (dld.selftest) in the real keychain. Paste the outputs.
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-1.2.md; PR "step-1.2: ...".
```

**Your check:** `npm --prefix desktop run dld -- secrets list` lists every name as missing (or present, for keys you've already set). Add a typo key to a copy of the example series and run `dld series check` on it: it should name the field.

#### Step 1.3 — Planner: dates, spreadsheet, media, `dld plan`
Builds: §S8, §S10 · Needs: 1.2, M10 · Unlocks: `dld plan` shows the next 14 days and their gaps

```text
/goal Step 1.3 — planner: filename dates, spreadsheet, media probe and `dld plan`. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D3, §S8, §S10) and Part 5 §A1, §A2, docs/rebuild/plan.md ("Workflow and UX" steps 1-2, "Keep, drop, add"), the legacy core/file_scanner.py, core/excel_parser.py, blueprints/media.py (scan filtering, L672-680) and the tests test_file_scanner_dates.py, test_file_scanner_names.py, test_excel_parser.py, test_excel_transcript_column.py, test_media_scan.py, and docs/rebuild/build-log/step-1.2.md.

BUILD
1. src/engine/planner/dates.ts: port parse_names and its helpers exactly (3, 4, 6 and 8 digit rules, windows inside longer names, plausibility, the MMDD/MDD year rule, ambiguous 6-digit names listed under both dates) with an injectable "today" and explicit day-of-month validation (no Date rollover, §S8). Port every legacy test case, with the clock pinned to the date each legacy test assumed.
2. src/engine/planner/sheet.ts: read the .xlsx with exceljs (cell values, not formulas): legacy header normalisation; date parsing order, reading date cells with UTC getters and serial numbers as Excel dates; last duplicate wins with a warning; tags split on commas. Rows keyed by ISO date through sheet.columns. A OneDrive file not yet downloaded or mid-sync is "not ready" (§S8): retried once after a pause, then a gap naming the path; never a crash.
3. src/engine/media.ts (§S10): probe(path) with mp4box.js (reading the header boxes, wherever moov sits) for .mp4/.mov; ffprobe JSON for other containers when installed; isVertical (rotation-aware); aspect within the series tolerance of 9:16; duration.
4. src/engine/planner/plan.ts: scanMedia(folder) (videos and thumbnails by the legacy extension lists) and planDays(series, from, n = 14): per day the video, thumbnail, row, title state, each destination's task state (from the DB), and gaps with how soon the day publishes (the earliest destination time). An ambiguous filename resolves to the date that has a sheet row (legacy rule); still ambiguous = a gap. Days upsert into the store with their files and row; no tasks yet.
5. CLI: `dld plan [--from YYYY-MM-DD] [--days 14] [--json]`: a table (date, video, thumbnail, row, title, one column per enabled destination) with the gap list under it. `dld probe-media <file>`.

TEST: every ported date case, plus Feb 30 and Apr 31 rejected; sheet parsing from an .xlsx written by exceljs inside the test (blank and duplicate headers, each date format, a date cell read in a New York TZ, a serial date, a duplicate date, tags); the not-ready path with a fake stat; probe on synthetic videos from ffmpeg lavfi (1080x1920, 1920x1080, rotated, moov at the end) when ffmpeg exists, else skipped with a reason; planDays gaps for a missing video, row and thumbnail and an ambiguous name.
CHECK: import a scratch copy of the series pointed at the M10 folder and sheet; run `dld plan` and paste the table and gaps. For one week, compare the dates each filename maps to with the legacy parse_names output (run the Python function directly, read-only).
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-1.3.md; PR "step-1.3: ...".
```

**Your check:** run `npm --prefix desktop run dld -- plan`. The next two weeks should match what you know is ready, and every gap it lists should be real.

#### Step 1.4 — Runner, connector interface, HTTP, `dld ship`
Builds: §S6, §S7, plan "Architecture" · Needs: 1.3 · Unlocks: `dld approve`, `dld ship --mode plan`, `dld status`, `dld pull`, `dld probe`

```text
/goal Step 1.4 — the job runner, connector interface, HTTP client and ship modes. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D5, D6, D10, D11, §S3, §S5, §S6, §S7) and Part 5 §A9, §A11, §A12, docs/rebuild/plan.md ("Architecture", "Workflow and UX" Ship), the legacy core/circuit_breaker.py, core/upload_jobs.py (waits, breaker use, ordering) and tests/test_circuit_breaker.py, and docs/rebuild/build-log/step-1.3.md.

BUILD
1. §S6 src/engine/http.ts: createClient(service, {baseUrl, auth}) over fetch: timeout; retries with exponential backoff + jitter on network errors, 408, 429, 5xx (Retry-After honoured); JSON helpers; file bodies streamed from disk (never buffered whole) with byte progress; redacted log lines; the plan-mode recorder; DLD_FAULT injection.
2. src/engine/connectors/types.ts and registry.ts: Destination, Artifact, Connector (as §S7), Issue (with fix), Produced (each artifact with origin created | found), Check, ProbeResult. DLD_FAKE_CONNECTORS=1 swaps in test/fakes.
3. src/engine/breaker.ts: port the legacy breaker (closed / open / half-open, one trial call; only transient errors count: network, timeout, session expired); one per connector, thresholds from the series.
4. src/engine/runner.ts: ship({series, dates, only?, mode}) creates tasks for day x enabled destination (drafts targets per D5; "plan" targets run as plan), orders them from needs/produces (a cycle is a config error), runs ready tasks (two uploads at once), parks waiting tasks and re-checks them on every ship until the cap (D10), calls find() before post() (found → recorded with origin found, no post), verify() after post(), retries under the breaker, records every transition and the mode, and emits typed progress events. A day with no approved title (D6) or a blocking preflight issue doesn't start. Takes the writer lock with a heartbeat. Cancel with an AbortSignal leaves unstarted tasks ready.
5. pull({dates, only?, mode}): drafts → connector.remove on origin created artifacts only; live → connector.unschedule (wired now, used from 3.1). The runner enforces the origin rule whatever the caller asks.
6. CLI: `dld approve <date> --title "..."` and `dld unapprove <date>` (1.5 adds suggestions); `dld ship <date|from..to> [--only a,b] [--mode plan|drafts|live]` (plan is the default; live asks you to type the date); `dld status <date>` (tasks, states, links with origin, last error); `dld pull <date> --mode drafts`; `dld probe [service...]`. doctor shows the probe summary.
7. test/fakes/: fake connectors with configurable needs, produces, delays, failures and find() hits.

TEST: order from declarations (shapes like YouTube → Rock Email and Wistia → Rock Daily) with independents in parallel; a wait that resolves on a later ship, and one that passes its cap (that task fails with the reason, the rest finish); a find() hit skips post and is recorded found; pull refuses found artifacts and live tasks; retries, then the breaker opens and that connector's other tasks fail fast; plan mode sends nothing (MockAgent with no interceptors); a killed run's running tasks come back ready on the next start; cancel; events in order.
CHECK: with DLD_FAKE_CONNECTORS=1, approve a fixture day, run `dld ship <day> --mode plan` and paste the event stream; run `dld probe` (real connectors: "not built yet").
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-1.4.md; PR "step-1.4: ...".
```

**Your check:** read the event stream in the log. Rock Email should start only after YouTube produced its link, and Rock Daily Experience only after Wistia.

#### Step 1.5 — Titles and text templates
Builds: §S9, §S12, plan "Workflow and UX" (approve, preview) · Needs: 1.4, M6 · Unlocks: `dld titles`, `dld preview`

```text
/goal Step 1.5 — title suggestions, approval and the text templates. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D6, §S9, §S12) and Part 5 §A3-§A8, docs/rebuild/plan.md ("Workflow and UX" approve and preview, "Keep, drop, add"), the legacy core/llm_title_gen.py, uploaders/rock/text.py, uploaders/youtube_uploader.py (L518-545), uploaders/vista_social_uploader.py (L738-758) and tests/test_rock_helpers.py, and docs/rebuild/build-log/step-1.4.md. Before writing the Claude call, load the claude-api skill and follow it for the TypeScript SDK and structured output.

BUILD
1. §S12 src/engine/titles.ts: suggestTitles(day, n = series.titles.count, default 5) from the day's transcript column, with the legacy prompt rules (Shorts style, under 60 characters) in prompts/titles.md; the transcript cut to 2000 characters at a word boundary at prompt time. Claude via @anthropic-ai/sdk with structured JSON output (model = series.titles.model, default claude-opus-5-5; key = secret anthropic.api_key); if series.titles.endpoint is set, a local OpenAI-compatible server instead. Cache keyed on the full transcript's hash in the DB. A missing transcript is an Issue, not a crash. Suggestions are stored on the day.
2. approve(day, title) / unapprove(day): the explicit Approved state (D6) with time and source (suggestion n | edited | sheet). Editing an approved title unapproves it. `dld approve` gains `<n>` to pick suggestion n.
3. §S9 src/engine/templates.ts: a small renderer ({{field}}; a missing field is an error naming the template and field) and named helpers: monthDayShort ("Dec 25"), monthDayLong ("December 25"), longDate ("December 25, 2026"), normalizeVerse (legacy normalize_vista_content), shortsDescription (legacy exactly: append "\n#Shorts" unless present, strip, then "\n\n" + footer only if the footer isn't blank), withFooter. Fill the example series' templates so the output equals the legacy text for: YouTube Short title, description, tags; the Vista caption; the Spotify title (episode-title column) and description; the Rock parent, Reflection, Vista and Spotlight titles and fields; the Rock Email title, message and SMS (their footers come from Rock in 1.8; placeholders until then).
4. preview(day): per destination exactly what will post: text fields, files, and the schedule time in New York and UTC.
5. CLI: `dld titles <date> [--refresh]`, `dld preview <date> [--json]`.

TEST: every legacy text case (test_rock_helpers.py plus the YouTube and Vista composition rules, including a blank footer) as golden tests through the templates; the Claude call with a faked client (valid output, invalid output then a clear failure, a refusal stop reason); cache hit, and a miss after an edit past character 2000; edit-after-approve unapproves; a fixture day's preview matches a stored snapshot.
CHECK: with the real key (M6), `dld titles` on one real day from the M10 sheet; paste the titles, the token usage and `dld preview` for that day. Without the key, run against the fake and list M6 under Manual items.
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-1.5.md; PR "step-1.5: ...".
```

**Your check:** read the five titles in the log: would you pick one? Compare `dld preview` with what the old app posted for the same day on YouTube, Vista Social and Rock: the text should match word for word, apart from the titles.

#### Step 1.6 — YouTube and Wistia connectors
Builds: YouTube Short, Wistia (plan "Platform by platform") · Needs: 1.5, M4, M5 · Unlocks: `dld ship <date> --only youtube,wistia --mode drafts`

```text
/goal Step 1.6 — YouTube Shorts and Wistia connectors. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D3, D5, D7, D9, D11, §S6, §S7) and Part 5 §A3, §A12, §B (YouTube, Wistia), docs/rebuild/access.md, docs/rebuild/plan.md ("Platform by platform", "Risks and open questions"), the legacy uploaders/youtube_uploader.py, core/quota.py, core/session_state.py (infer_wistia_ref), tests/test_youtube_retry.py and tests/test_quota.py, and docs/rebuild/build-log/step-1.5.md.

BUILD
1. `dld connect youtube`: the installed-app OAuth flow with google-auth-library (loopback redirect on 127.0.0.1, PKCE, offline access, scope youtube) using the Desktop client in secret youtube.client (M5); the refresh token goes to youtube.token. probe = channels.list mine=true (1 unit), naming the channel.
2. src/engine/connectors/youtube.ts over §S6 (D3): post = YouTube's resumable upload protocol for videos.insert (start with uploadType=resumable, PUT the file in chunks that are multiples of 256 KiB, handle 308 and Range, after a failure ask the session URI for its offset with Content-Range "bytes */N" and resume); title = the approved title; description and tags from 1.5's templates; categoryId and madeForKids from the series. live: privacy private + publishAt (series time, New York → UTC, "%Y-%m-%dT%H:%M:%S.0Z"). drafts: private, no publishAt. Legacy retries (408/429/5xx, 5 tries). produces youtube.videoId and youtube.url (https://www.youtube.com/shorts/<id>, legacy). find (D11): live = the day's video among recent uploads (uploads playlist, 1-unit calls, title + date window); drafts = only videos dld created in drafts mode, by id. verify = videos.list: title, privacy, publishAt. remove = delete (drafts, origin created). unschedule = privacy private, publishAt cleared. preflight: token alive, 9:16, ≤ 180 s, title ≤ 100 chars, quota.
3. src/engine/quota.ts: the ledger in the quota table, day = America/Los_Angeles, costs and cap from the series; a call that would pass the cap is refused with the first day it fits; charged after success (legacy).
4. src/engine/connectors/wistia.ts: confirm every endpoint in Wistia's docs first (rule 2). post = upload of the same file (streamed from disk, progress) with Bearer wistia.token into the series project; drafts = the drafts project (M4), and a drafts write to the live project throws (D5); name = "app YYMMDD" from the day's date (log any day where the legacy infer_wistia_ref on its filename differs). produces wistia.hashedId and wistia.name. find = medias in that mode's project with that name; verify = exists, name, processing finished (polled, cap from the series); remove = delete (drafts, origin created); probe = read both projects.
5. Both connectors stream from the same file path: no copies.

TEST (MockAgent): the resumable protocol (chunk sizes, 308 + Range, resume after a dropped connection via DLD_FAULT); publishAt across DST; drafts send no publishAt; quota refusal message; find hit skips the upload; drafts find ignores a live video with the same title; Wistia request shape and Bearer header; the live-project guard; name format; verify polling; remove refuses found and live.
CHECK: `dld probe youtube wistia`. On one real day (M10 video): `dld ship <date> --only youtube,wistia --mode drafts`, then `dld status <date>`. Paste the links, timings and quota used. Leave the drafts for Ryker; list them under Manual items.
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-1.6.md; PR "step-1.6: ...".
```

**Your check:** open both links. On YouTube Studio the Short is private with no schedule, with the right title, description and #Shorts. In Wistia the video sits in the test project, named `app YYMMDD`.

#### Step 1.7 — Vista Social connector
Builds: Vista Social (plan "Platform by platform") · Needs: 1.6, M3 · Unlocks: Vista Social drafts by API, or a recorded move to the browser in 2.4

```text
/goal Step 1.7 — Vista Social connector (Instagram + Facebook). Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D4, D5, D11, §S6, §S7) and Part 5 §A6, §B (Vista Social), docs/rebuild/access.md (M3), docs/rebuild/plan.md ("Platform by platform", "Risks and open questions"), the legacy uploaders/vista_social_uploader.py, and docs/rebuild/build-log/step-1.6.md.

First read M3 in access.md. If the API add-on was declined, build nothing: record in the log and the tracker that Vista Social becomes a browser connector in step 2.4, run the gates and stop.

BUILD (API approved)
1. Confirm from Vista Social's API reference, then with read-only calls using secret vista.api_key: authentication; how profiles are listed and identified; how a video is attached (upload, upload endpoint, or public URL only); whether a post can be saved as a draft; how it is scheduled; the time format and zone; Reels support for Instagram and Facebook; how to read a post back and unschedule it. Write the answers into guide Part 5 §B (rule 2).
2. src/engine/connectors/vista.ts: post = one post to the series' Facebook and Instagram profiles (resolved by name at probe time, ids cached in the series), caption from 1.5's template, the vertical video, scheduled at the series time (New York). drafts = a Vista draft; if Vista has no drafts, the series' drafts target for Vista is "plan" (D5) and the log says so. If the API takes media only by public URL, use the Wistia media's original file URL (add the Wistia artifact to needs) and log the decision. produces vista.postId (and a URL if any). find (D11): live = a post in the day's window whose caption starts the same; drafts = only drafts dld created. verify = profiles, time and status read back. remove (drafts, created) and unschedule per the API; probe = list profiles.
3. preflight: both profiles connected; the caption within each network's limit (Instagram 2,200 characters); video limits from Part 5 §B.

TEST (MockAgent): request shape; profile resolution; caption composition; schedule conversion across DST; the media-by-URL path; a find() hit; drafts find ignores live posts; a verify mismatch reported; remove refuses found and live.
CHECK: `dld probe vista`; ship one real day with `--only vista --mode drafts` and paste the result. Ryker checks the draft in Vista Social (Manual item).
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-1.7.md; PR "step-1.7: ...".
```

**Your check:** in Vista Social, the draft is there for both Facebook and Instagram (Reel), with the caption and footer, at the right time. If the add-on was declined, the tracker says so and points at 2.4.

#### Step 1.8 — Rock REST client and Rock Email / SMS
Builds: Rock Email / SMS (plan "Platform by platform") · Needs: 1.6, M1, M2 · Unlocks: Rock Email drafts by REST

```text
/goal Step 1.8 — Rock REST client and the Rock Email / SMS connector. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D5, D10, D11, §S6, §S7) and Part 5 §A5, §A11, §B (Rock), docs/rebuild/access.md (M1, M2), docs/rebuild/plan.md ("Platform by platform", "Risks and open questions"), the legacy uploaders/rock/ (email.py, text.py, fields.py, constants.py; client.py for what each field is, L370-415), tests/test_rock_email.py, and docs/rebuild/build-log/step-1.6.md.

BUILD
1. Confirm against the Rock REST docs and the server (read-only, secret rock.rest_key, header Authorization-Token): the Rock version and which API (v1 /api/... or v2) fits; how to read a content channel's item attributes (keys, field types, default values); create and update ContentChannelItems (including StructuredContent where a channel uses the structured editor); set attribute values; upload a binary file for an image or file attribute; read an item back. Write what you find into guide Part 5 §B (rule 2). Use M2's drafts server or channels for every write; the live channels are read-only in this step.
2. src/engine/rock/client.ts: a typed client over §S6 for those calls, plus attributeMap(channel), which resolves attribute keys from the field names the legacy forms use (Email Message, SMS, YouTube Link, Thumbnail, Sent) and caches them in the series. A drafts-mode write to a live channel id or GUID throws (D5). The code holds no LCBC ids (rule 11).
3. src/engine/connectors/rock-email.ts: needs youtube.url, description, the email thumbnail. post = one item per date (drafts: M2's target): title "Daily Life {Month} {d}, {YYYY}"; start = the day; Email Message = description + "\n\n" + the Email Message attribute's default value; SMS = description + "\n\n" + the SMS attribute's own default value (legacy reads each box's pre-filled text); YouTube Link; Thumbnail = the uploaded file; Sent = No. produces rock.emailItemId. find (D11): an item in that mode's channel with that title (legacy skip rule). verify = title, date, link, Sent read back. remove = delete (drafts, created). unschedule = per M1's answer on what stops a send. probe = read the channels and attributes.
4. The wait: Rock Email waits for youtube.url (series cap, default 7200 s, legacy). If YouTube fails or isn't in this run, it uses the day's recorded YouTube URL from an earlier run, else fails with the reason.
5. Update examples/daily-life.series.json with the confirmed keys and ids.

TEST (MockAgent): attribute map from a recorded channel response (every id and value replaced, rule 7); message and SMS composition with their own defaults; the live-channel guard; a find() hit; wait release when YouTube fails; binary upload request shape; remove refuses found and live.
CHECK: `dld probe rock`; on one real day ship `--only youtube,rock-email --mode drafts` and paste the item link. Ryker compares it with an item the old app made (Manual item).
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-1.8.md; PR "step-1.8: ...".
```

**Your check:** in Rock (on M2's drafts target), the new email item and one the old app made look the same field by field: title, date, message and SMS with their footers, YouTube link, thumbnail, Sent = No.

#### Step 1.9 — Rock Daily Experience and stock photos
Builds: Rock Daily Experience (plan "Platform by platform"), §S13 · Needs: 1.8, M7 · Unlocks: all API destinations

```text
/goal Step 1.9 — Rock Daily Experience connector and stock photos. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D5, D10, D11, §S7, §S13) and Part 5 §A5, §A10, §B (Rock, Rock media sync), docs/rebuild/access.md, docs/rebuild/plan.md ("Platform by platform", "Risks and open questions"), the legacy uploaders/rock/ (orchestrator.py, client.py, fields.py, text.py), core/image_gatherer.py, tests/test_rock_helpers.py, and docs/rebuild/build-log/step-1.8.md.

BUILD
1. §S13 src/engine/photos.ts: port image_gatherer: keywords from the verse and topic through §S12's model (legacy prompt rules, prompts/photo-keywords.md); Unsplash then Pexels; portrait; legacy filters and size cap; no photo reused within 60 days or topic within 14 (photos table); the Unsplash download ping. Credits appended to credits/credits-YYYY-MM.tsv with the legacy columns. swap(day) takes the next candidate. Keys: unsplash.access_key, pexels.api_key.
2. Find out on the server (read-only) how a Spotlight item stores its video: the attribute's field type and stored value on an item the old app made. The legacy form's Media Account → Folder → Element pickers suggest Rock's built-in media elements (a Wistia media account synced by a Rock job; readable over REST, e.g. MediaElements), not a plugin; confirm which, how often the sync runs, and whether REST can trigger it. Record it in Part 5 §B and access.md.
3. src/engine/connectors/rock-daily.ts: needs the row (passage, scripture, prayer, episode title), the photo and the Wistia media; then waits until Rock's synced media in that mode's folder contains the day's "app YYMMDD" (D10: across runs, cap from the series; trigger the sync if REST allows it). post creates the Reflection, Vista and Spotlight children, then the parent; sets Content and StructuredContent where the channel uses the structured editor; sets every attribute per Part 5 §A5 (titles, Pause & Pray, prompt fields, taglines, Vertical, likes, background image upload, Enable Prayer false, Prayer Count 3); links the children to the parent (ContentChannelItemAssociations); active date = the day. A failure after the first create removes what this run created, so a retry starts clean. drafts: M2's target, with the Spotlight pointing at the drafts Wistia folder; the live-channel guard from 1.8. produces rock.parentId and the three child ids. find (D11): a parent for the date in that mode's channel (legacy skip rule). verify = all four items and their links read back. remove = delete all four (drafts, created). unschedule = make them inactive (per what step 2 finds).
4. preview (1.5) shows the photo and its credit; `dld photo <date> [--swap]`.

TEST (MockAgent; fixtures from item 2 with every id and value replaced): photo search fallbacks and no-repeat windows; create order and attribute writes; association calls; cleanup after a mid-run failure; the sync wait (found on a later run, cap reached); the live-channel guard; a find() hit; remove refuses found and live.
CHECK: `dld probe rock`; ship one real day with `--only wistia,rock-daily --mode drafts`; paste the timings (upload, sync wait) and item links. Ryker compares the four items with ones the old app made (Manual item).
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-1.9.md; PR "step-1.9: ...".
```

**Your check:** on M2's drafts target, the parent and its three children match an old-app day field by field. The Spotlight item plays the right Wistia video. The log shows how long Rock's sync took.

#### Step 1.10 — Gate 1: one real day shipped as drafts
Builds: G1 (plan "Roadmap") · Needs: 1.6–1.9 · Unlocks: Phase 2

```text
/goal Step 1.10 — Gate 1: one real day shipped as drafts, end to end. Read docs/rebuild/build-guide.md Parts 1.3, 1.5 and 2, docs/rebuild/plan.md ("Workflow and UX", "Roadmap"), docs/rebuild/access.md and the build logs of steps 1.6-1.9.

BUILD
1. Fix what the run below exposes, each fix with a regression test, in the service that owns it (rule 4).
2. `dld ship` ends with a summary: per destination the state, link, origin, time taken, retries, and the waits (Rock Email for YouTube; Rock Daily for Wistia + Rock's sync).
3. docs/rebuild/runbook.md: shipping a day from the CLI (plan → titles → approve → preview → ship --mode drafts → status → pull), each failure message with its fix, where logs and evidence live.

CHECK (this Mac; a real upcoming day from M10; YouTube, Wistia, Vista Social if 1.7 built it, Rock Daily Experience, Rock Email; every write to drafts targets):
- `dld probe` all green; `dld plan` shows the day ready.
- `dld titles`, `dld approve`, `dld preview`; then `dld ship <date> --mode drafts`. Every artifact has origin created; if any was found instead, stop and explain why.
- `dld status <date>`: every task scheduled or verified (Rock Daily may be waiting on the sync; ship again later until it isn't). Ship again: every task is found and nothing posts twice.
- On another day, interrupt the run mid-upload (Ctrl-C) and rerun: it resumes with no duplicates.
- Record each destination's wall time, the total, the quota used and every link in the log.
- Leave the drafts for Ryker. The log lists what to look at, and the `dld pull <date> --mode drafts` commands that remove them after review.
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-1.10.md; PR "step-1.10: ...". Mark G1 in the tracker "awaiting Ryker's review".
```

**Your check:** open every link in the log. G1 passes when each draft is what you would have posted by hand. Mark G1 passed in the tracker, then run the pull commands.

### Phase 2 — App

#### Step 2.1 — Electron shell, engine process, series setup
Builds: §S14, plan "Architecture" and "Workflow and UX" (set up once) · Needs: G1 · Unlocks: the app opens; the series is set up in the UI

```text
/goal Step 2.1 — Electron shell, the engine in a utility process, and series setup. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D2, D3, D7, D8, §S1, §S3, §S14), docs/rebuild/plan.md ("Architecture", "Tech stack", "Workflow and UX" set up once) and the build logs of steps 1.2 (the SQLite decision) and 1.10.

BUILD
1. Add Electron (the version 1.2 checked), electron-vite and React to desktop/; electron-builder config without signing (3.2 signs). src/main: one window; contextIsolation on, nodeIntegration off, a sandboxed renderer, a strict CSP, no remote content; single-instance lock. `npm --prefix desktop run dev` starts it.
2. Confirm 1.2's SQLite choice works in the utility process, the CLI and Vitest after a clean `npm ci`; fix the scripts if not, and record it.
3. §S14 src/engine/api.ts: the facade. The engine runs in an Electron utilityProcess; main brokers a MessagePort; preload exposes a typed window.dld with only the facade. The CLI keeps calling it in-process. The writer lock (§S3) gives a clear message when the CLI and the app collide.
4. During a ship the Mac stays awake: powerSaveBlocker in the app, caffeinate in the CLI (D7).
5. Renderer: the app frame with Week, Connections, Settings and Activity. Settings → Series: media folder and spreadsheet pickers (native dialogs), sheet and column mapping with a live preview of the first rows, publish times per destination, destination switches, drafts targets (D5); validated by §S2, saved through the facade; import / export of the series JSON.
6. Activity: the live event stream, filterable, with a Copy button.

TEST: facade contract tests give the same results in-process and over a MessagePort; preload exposes nothing else; the writer lock; Playwright's Electron support launches the app, opens Settings, changes a publish time, and the DB has it.
CHECK: run the app on this Mac and set up the Daily Life series from the M10 copy through the UI. Save a screenshot of Settings in the data dir (path in the log); `dld series show` shows the same values.
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-2.1.md; PR "step-2.1: ...".
```

**Your check:** `npm --prefix desktop run dev` opens the app. Change a publish time in Settings, quit, reopen: the change is still there, and `dld series show` agrees.

#### Step 2.2 — Week board, approvals, preview
Builds: plan "Workflow and UX" (watch the week, approve, preview) · Needs: 2.1 · Unlocks: the week board you keep open

```text
/goal Step 2.2 — the week board, title approval and preview. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D6, D10, §S8, §S9, §S12, §S13, §S14), docs/rebuild/plan.md ("Workflow and UX": watch the week, approve titles, preview) and docs/rebuild/build-log/step-2.1.md.

BUILD
1. Week board (the home screen): the next 14 days, scrollable back and forward; a row per day: date, video, thumbnail, sheet row, title state, then one status chip per enabled destination (task state; a wait reads "waiting for YouTube"); gaps inline with how soon the day publishes. Live updates: the engine watches the media folder and the spreadsheet (debounced; a OneDrive file not yet downloaded is a gap, §S8).
2. Day panel: the transcript excerpt, the suggestions (§S12) with Pick, an edit box, Approve / Unapprove (D6), Refresh suggestions.
3. Preview: one card per destination showing exactly what will post (§S9 preview): the YouTube title and description, the Instagram / Facebook caption, the Spotify episode, the email and SMS draft, the three Rock items with the stock photo, its credit and a Swap button (§S13); schedule times in New York.
4. Keyboard: arrows move between days; Enter approves the picked suggestion. Empty, loading and error states for every panel.

TEST: the board model from fixtures (gaps, waits, states); approve / unapprove round trip through the facade; watcher debounce; a Playwright Electron test that approves a title, swaps a photo and sees the preview change.
CHECK: run the app on the real series; approve the titles of two upcoming days in the UI; screenshots of the board and one preview in the data dir (paths in the log); the previews match `dld preview` for the same days.
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-2.2.md; PR "step-2.2: ...".
```

**Your check:** use the board for a few minutes as you would on a Monday. Can you see at a glance which days are ready? Approve a title with the keyboard. Does each preview card show what you expect to post?

#### Step 2.3 — Preflight, Ship, progress lanes, notifications
Builds: plan "Workflow and UX" (preflight, ship, hear about it) · Needs: 2.2 · Unlocks: shipping a selection of days from the app (drafts)

```text
/goal Step 2.3 — preflight, the Ship button, progress lanes and notifications. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D5, D9, D10, §S7, §S14), docs/rebuild/plan.md ("Workflow and UX": preflight, ship, hear about it) and docs/rebuild/build-log/step-2.2.md.

BUILD
1. Preflight for a selection of days: each connector's preflight plus the runner's (approved title; files present and readable; 9:16 and ≤ 180 s via §S10; title and caption limits per platform; times in the future; every token and login alive via probe; quota enough for the selection, else which day each upload fits, D9). Each failure shows its fix, as a button where one exists (open Settings, reconnect, swap photo, reveal the file in Finder).
2. Ship: one button for the selection, disabled while preflight has blocking issues; mode drafts (D5; the switch to live is 3.3). One progress lane per destination with byte progress for uploads, waits explained, retries and breaker state; Cancel.
3. A failed task shows its error, its evidence link (browser tasks, 2.4) and Retry; needs_login shows Reconnect (2.4 fills it in).
4. Notifications: an OS notification when a run ends (counts, failures) and when a waiting task resolves or passes its cap. Clicking it opens the board on that day. (Email or Slack summaries are Later.)

TEST: the preflight model (each issue kind and its fix action); ship disabled with blocking issues; lanes from a recorded event stream; cancel; notification payloads.
CHECK: in the app, ship two approved real days as drafts; screenshot the lanes mid-run and the finished board (paths in the log); then pull those drafts from the CLI.
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-2.3.md; PR "step-2.3: ...".
```

**Your check:** break something on purpose (rename a day's video) and open preflight: the issue should say what's wrong and how to fix it. Fix it, then Ship two days as drafts and watch the lanes.

#### Step 2.4 — Browser runner, Connections, login health, assisted mode
Builds: §S11, plan "Workflow and UX" (login health, assisted mode, failure evidence) · Needs: 2.1, M3 · Unlocks: in-app logins; Vista Social by browser if the add-on was declined

```text
/goal Step 2.4 — browser runner, Connections page, login health and assisted mode. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D4, D5, §S5, §S11, §S14) and Part 5 §A6, docs/rebuild/plan.md ("Workflow and UX": new features), docs/rebuild/access.md (M3), the legacy core/playwright_session.py and uploaders/vista_social_uploader.py, and the latest build log in docs/rebuild/build-log/.

BUILD
1. §S11 src/engine/browser/: playwright-core with the installed Google Chrome (channel chrome), one persistent profile per platform in profiles/<platform>, headed; one task at a time per profile; withPage(platform, fn) with timeouts; on failure a screenshot, DOM snapshot and URL in evidence/<task id>/, secrets redacted (§S5).
2. Login per §S11: connect(platform) opens plain Chrome (no automation) on that profile directory at the platform's login page, so Google or SSO sign-in and 2FA work; when the person closes it, checkLogin confirms a logged-in page through Playwright. Nothing from the session leaves the profile.
3. Connections page: every service (API keys and OAuth through §S4 and the probes; browser logins) with status, last check, Connect / Reconnect, Remove. A daily timer and a check before every run; a lapsed login marks the affected tasks needs_login before the run starts, never mid-batch.
4. Assisted mode: assist(platform, prefill) opens the platform's form filled from the preview and waits for the person to publish; then find / verify record the result. Connectors fall back to it when their automation fails (a per-connector setting, default on); Rock and Vista API failures can open their platform form the same way.
5. Only if M3 declined the Vista API: src/engine/connectors/vista-browser.ts ported from the legacy uploader (Facebook + Instagram only, YouTube off; caption; the vertical video and its long media wait; schedule in New York with the legacy date, hour, minute and AM/PM controls; drafts = save as draft) with find / verify from the Vista calendar.

TEST: against small hand-written HTML pages served locally (rule 6), never saved real pages: profile isolation; evidence written and redacted on a forced failure; checkLogin (logged in, login wall); login health marks tasks needs_login before a run; the assisted flow; vista-browser's form steps if built.
CHECK: connect each browser platform on this Mac (Ryker signs in when the window opens; if he isn't there, list it as a Manual item); run the login check; screenshot the Connections page into the data dir (path in the log).
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-2.4.md; PR "step-2.4: ...".
```

**Your check:** open Connections. Every service shows green after you sign in where asked. Sign out of one platform in its window, run the login check, and the board should flag the affected days before you ship.

#### Step 2.5 — Spotify for Creators connector
Builds: Spotify (plan "Platform by platform") · Needs: 2.4, M8 · Unlocks: all six destinations

```text
/goal Step 2.5 — Spotify for Creators connector. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D4, D5, D11, §S11) and Part 5 §A7, docs/rebuild/access.md and the M8 recording in docs/rebuild/spotify/, docs/rebuild/plan.md ("Platform by platform", Q&A on Spotify title and vertical video), the legacy uploaders/simplecast_uploader.py (waits, schedule math, draft fallback) and tests/test_simplecast_schedule_math.py, and docs/rebuild/build-log/step-2.4.md.

BUILD
1. From the recording write docs/rebuild/spotify/flow.md: each screen, stable selectors (roles and labels first, test ids next, never generated class names), the waits, and what "saved as draft", "scheduled" and "published" look like. No account names, emails or ids in it (the repo is public).
2. src/engine/connectors/spotify.ts on §S11: post = a new video episode on the series' show: the vertical file; title = the episode-title column; description from 1.5's template; scheduled at the series time in New York (port the legacy schedule math where Spotify's picker needs rounding); drafts = save as draft. Wait for upload and processing with progress. produces spotify.episodeUrl (or id). find (D11): the show's episode list for that title and date; in drafts mode only drafts dld created. verify = title, scheduled time, status. remove = delete the draft (drafts, created); unschedule = back to draft. When automation fails, fall back to assisted mode (2.4) with the form filled in.
3. Selector drift is the main risk: keep every selector in one file with the date it was checked, and add a probe that opens the new-episode form without saving, run by the daily login check, so drift shows up before a run.

TEST: against small hand-written HTML pages per screen, built from flow.md (rule 6); schedule rounding cases; the draft and schedule paths; assisted fallback on a missing selector.
CHECK: on this Mac with the logged-in profile: `dld probe spotify`; ship one real day with `--only spotify --mode drafts`; screenshot the draft into the data dir (path in the log); record upload and processing time.
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-2.5.md; PR "step-2.5: ...".
```

**Your check:** in Spotify for Creators the draft episode has the vertical video, the episode title from the sheet and the description. Its schedule is right when you open it. Delete the draft yourself or with `dld pull`.

#### Step 2.6 — Gate 2: a full week as drafts
Builds: G2 (plan "Roadmap") · Needs: 2.3–2.5 · Unlocks: Phase 3

```text
/goal Step 2.6 — Gate 2: a full week shipped as drafts from the app. Read docs/rebuild/build-guide.md Parts 1.3, 1.5 and 2, docs/rebuild/plan.md ("Workflow and UX", "Roadmap", "Risks and open questions"), docs/rebuild/runbook.md and the build logs of steps 2.1-2.5.

BUILD
1. Fix what the run exposes, each fix with a regression test, in the owning service.
2. Update docs/rebuild/runbook.md for the app: the week flow (open, approve, preview, preflight, Ship, read the board), each failure and its fix, assisted mode, reconnecting a login.

CHECK (this Mac; the next 7 real days; all six destinations on their drafts targets; from the app, driven by Playwright's Electron support where possible and by Ryker where a login needs him):
- After approvals the board shows all 7 days ready and preflight is clean.
- Ship all 7 as drafts. Record per destination the wall time, retries, waits and quota (7 uploads either fit the day's quota or the app says which day each goes on, D9).
- Drill one failure of each kind with DLD_FAULT (a network drop mid-upload, a 5xx storm that opens a breaker) and two by hand (a login signed out in its window, a missing thumbnail); record how the app reported each and recovered. Never change system network settings.
- Ship the week again: nothing duplicates.
- The log lists every draft for Ryker's review and the pull commands.
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-2.6.md; PR "step-2.6: ...". Mark G2 in the tracker "awaiting Ryker's review".
```

**Your check:** spend a normal Monday's routine in the app for next week, with drafts. G2 passes when every draft is right and the routine took less time than the old app. Mark G2 in the tracker, then pull the drafts.

### Phase 3 — Cutover

#### Step 3.1 — Verify and Pull a day
Builds: plan "Workflow and UX" (verify, pull a day), "Keep, drop, add" (calendar refresh → verify) · Needs: G2 · Unlocks: a board that shows what is really on each platform

```text
/goal Step 3.1 — Verify after every run, and Pull a day. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D10, D11, §S7, §S14) and Part 5 §A13, docs/rebuild/plan.md ("Workflow and UX": verify, pull a day; "Keep, drop, add": calendar refresh), the legacy core/calendar_refresh.py and core/refresh/, and docs/rebuild/build-log/step-2.6.md.

BUILD
1. After every run, and on a timer (hourly for waiting tasks, daily for the rest), the engine re-checks waiting tasks (D10) and runs each connector's verify on scheduled tasks for the last 7 and next 14 days (title, scheduled time, visibility read back). Match → verified. Mismatch → a flagged chip with the difference and actions (re-ship, accept, open on the platform). A task whose item is gone from the platform returns to ready with the reason (D11). The legacy refresh covered 30 days back and 180 forward; this narrower window is deliberate (the board is a working view), and the month view reads stored states.
2. This replaces the legacy calendar refresh: the board's chips are the calendar, and a month view reads the same tasks.
3. Pull a day: from the day panel, unschedule that date on every platform (connector.unschedule: YouTube back to private with no publishAt, Vista unscheduled, Rock items inactive, Spotify back to draft; Wistia untouched) behind a confirmation that lists exactly what changes, including items with origin found. Nothing is deleted. Anything that can't be unscheduled is listed with a link for a person to do it. The CLI's `dld pull <date> --mode live` requires typing the date.
4. YouTube verify uses only 1-unit calls, counted in the quota ledger.

TEST: verify outcomes per connector against mocked platforms (match, title changed, time changed, gone); the timers; the pull confirmation's contents; live pull unschedules and never deletes; it runs only after confirmation.
CHECK: ship two days as drafts; change one draft's title through the API; run verify and show the mismatch on the board (screenshot in the data dir, path in the log); pull the other drafts day from the UI and confirm with `dld status`.
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-3.1.md; PR "step-3.1: ...".
```

**Your check:** edit a draft's title by hand on YouTube Studio, then wait for verify (or run it). The board flags that day and shows the difference.

#### Step 3.2 — Signed universal build and auto-update
Builds: plan "Tech stack" (packaging, updates), "Risks" (desktop distribution) · Needs: 3.1, M11 · Unlocks: an installable, auto-updating app

```text
/goal Step 3.2 — signed, notarized universal build with auto-update. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D2, D3) and Part 5 §B (Apple, electron-updater), docs/rebuild/plan.md ("Tech stack", "Risks and open questions"), the legacy .github/workflows/release-agent.yml (the repo's release habits only), and docs/rebuild/build-log/step-3.1.md.

BUILD
1. electron-builder: a macOS universal (arm64 + x64) dmg and zip; appId; product name "Daily Life Distributor"; hardened runtime with minimal entitlements; any native module rebuilt for both architectures and merged by @electron/universal (set x64ArchFiles for files identical in both). No bundled ffprobe (D3). desktop/THIRD_PARTY.md lists every runtime dependency with its license.
2. Signing and notarization with the Developer ID (M11): the certificate and notarization credentials only as GitHub Actions secrets (names in the log); a local unsigned build still works for development.
3. electron-updater from this repo's GitHub Releases: check at launch and daily, download in the background, install on quit, never during a run. Settings → About shows the version and update state.
4. .github/workflows/release-desktop.yml: on tags desktop-v*, build, sign, notarize, staple and publish the release with latest-mac.yml; on pull requests touching desktop/ or the workflow, the same build with --publish never (signing only when the secrets exist). Version from desktop/package.json.
5. First run: with an empty data dir, offer to import a series JSON.

TEST: a snapshot of the builder config; updater logic with a fake feed (no update; update available; downloaded, then installed on quit and never during a run).
CHECK: build locally unsigned and launch the packaged app on this Mac (both architectures present: `lipo -info` on the binary). The PR run of release-desktop.yml builds, and with M11's secrets signs and notarizes. Publishing a release is Ryker's Manual item: the log gives the exact tag command.
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-3.2.md; PR "step-3.2: ...".
```

**Your check:** after merging, push the tag from the log (`desktop-v0.1.0`). Install the dmg from the release and open it: macOS shouldn't warn about the developer. Then release `desktop-v0.1.1`: the installed app should update itself on its next quit.

#### Step 3.3 — Legacy import, side-by-side compare, the switch to live
Builds: plan "Risks" (rewriting a working tool), "Roadmap" (run beside the old app) · Needs: 3.2, M12 · Unlocks: the two weeks side by side and the switch (M13)

```text
/goal Step 3.3 — legacy import, the side-by-side compare and the switch to live. Read docs/rebuild/build-guide.md Parts 1.3, 1.5 and 2 (D5, D11, D12), Part 4 M12-M13, docs/rebuild/plan.md ("Risks and open questions", "Roadmap"), the legacy core/db.py (upload history, image_history) and core/image_gatherer.py (credits), and docs/rebuild/build-log/step-3.2.md.

BUILD
1. `dld import-legacy <db copy> [--credits <dir>]`, read-only on the copy Ryker made outside the repo (M12): only the image history (so the no-repeat windows continue), the credits files, and per-date upload history as hints for find() (the platform stays the truth, D11). It never reads the secrets table. Prints what it imported.
2. `dld compare <date>` and a Compare view on the board: per destination, the new app's draft vs what the old app put live on the platform (title, time, text, files), with differences highlighted, for the daily side-by-side check.
3. The switch to live (plan: drafts until two clean weeks): Settings → ship mode, drafts | live, for the whole series at once, live only after a confirmation that names the date of the first live day; recorded with who and when. Rollback: any destination can be switched off in the new app (the old app takes it back by hand).
4. docs/rebuild/cutover.md:
   - the two weeks: the old app posts live every day; the new app ships the same days as drafts; `dld compare` each day; G3 = 14 days in a row where every destination's draft matched (or the difference was the new app being right), with no manual fixes beyond approvals.
   - switch day: in the old app, untick every platform; tell staff the app now uploads to Wistia (no manual upload, or "app YYMMDD" duplicates break Spotlight); in the new app, switch to live; watch the first live week.
   - rollback per destination, and the timing of 3.4 and M14 (after the first clean live week).

TEST: import from a synthetic legacy DB built in the test from the legacy schema (secrets table present and untouched); compare output; the switch's confirmation and record; switching a destination off.
CHECK: run import-legacy on the M12 copy and `dld compare` on yesterday's date; paste both outputs.
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-3.3.md; PR "step-3.3: ...".
```

**Your check:** read `docs/rebuild/cutover.md` and agree to it, then start M13.

#### Step 3.4 — Retire the legacy app
Builds: plan lead ("retire the hosted web app, the agent relay and the VPS"), "Keep, drop, add" (drops) · Needs: M13 (G3 passed, switched to live, one clean live week) · Unlocks: M14

```text
/goal Step 3.4 — retire the legacy app from the repo. Read docs/rebuild/build-guide.md Parts 1.3 and 2 (D12), docs/rebuild/cutover.md and docs/rebuild/build-tracker.md: G3 must be passed and the first live week marked clean by Ryker; if not, stop and say so. Then read CLAUDE.md and README.md.

BUILD
1. One PR removes the legacy Python app: app.py, blueprints/, core/, uploaders/, agent/, templates/, static/, the Python tests/, deploy/, bin/, launch_mac.command, agent.spec, requirements*.txt, pytest.ini, config.yaml, .dockerignore, every scripts/ file except check_prompts.py, and .github/workflows/ci.yml and release-agent.yml. Useful history moves to docs/legacy/ (RUNBOOK.md, the superpowers specs, a one-page summary of the legacy database tables import-legacy reads). Nothing under docs/rebuild/ is deleted.
2. README.md rewritten for the desktop app: what it does, install from Releases, first run, the week flow, where data lives, building from source.
3. CLAUDE.md rewritten for the new codebase: layout, commands, gates, the release flow, pointers to this guide; the legacy content goes.
4. desktop/ stays where it is (docs and CI paths stay valid).

TEST: the full desktop suite; `git grep` finds no reference to removed paths outside docs/legacy/ and docs/rebuild/.
CHECK: from a fresh clone: npm ci, check, dld doctor and a local package build all pass; desktop CI green.
DONE: Gates: guide 1.3 rule 9. Write docs/rebuild/build-log/step-3.4.md; PR "step-3.4: ...". Manual item: M14.
```

**Your check:** read the PR's file list: only legacy files go. After merging, the repo's front page reads as the desktop app. Then do M14.

### Later (after cutover, no prompts yet)

From the plan's "Roadmap" and "New features worth building". Write a prompt in the same template when you schedule one.

| Feature | Scope | Builds on |
|---|---|---|
| Edit once, update everywhere | `connector.edit`: change a title, caption or time after shipping and push it to YouTube, Wistia, Rock, Vista Social; Spotify through automation | §S7, 3.1 |
| Template editor | Live preview per platform for titles, descriptions and footers; export / import a whole series setup | §S2, §S9 |
| Run summaries | Optional email or Slack summary with every link (plan "Hear about it"); OS notifications already ship in 2.3 | 2.3 |
| Spreadsheet write-back | Approved titles and per-platform links back into the OneDrive sheet through Microsoft Graph (needs a Microsoft app registration) | §S8 |
| Station mode | An always-on studio Mac watches the folder and ships each approved day as its files arrive (D6 still applies) | §S7, 2.3 |
| A second series | Config only (rule 11); proves the series model | §S2 |
| Other churches | License key, onboarding, per-church templates; only if that becomes real (plan Q&A: "maybe later") | 3.2 |

---

## Part 4 — Manual items

Everything a session can't or mustn't do. Each step's **Needs** line says which of these must come first. Record non-secret answers in `docs/rebuild/access.md`. The repo is public: never put keys, private URLs or account emails there.

| # | When | What |
|---|---|---|
| **M0** | First | **Put these docs on `main`.** Review `docs/rebuild/` (plan, guide, tracker, access) and `scripts/check_prompts.py`, then commit them to `main` (a "docs: rebuild plan and build guide" PR, or ask Claude to do it). Every session reads them from `main`. |
| **M1** | Start now; before 1.8 | **Rock REST key and how Rock sends.** Ask LCBC's Rock admin for: a REST key (a Rock "REST Key" person) with a security role that can read and edit the five Daily Life content channels (parent, Reflection, Vista, Spotlight, Email) and upload binary files; the Rock version. Ask too: what sends a Daily Life email / SMS item (a job that picks up items with Sent = No on their start date?), and what stops it; whether the Spotlight video picker is Rock's built-in media (a Wistia media account) and how often its sync job runs, and whether it can be triggered. After 1.2, store the key with `dld secrets set rock.rest_key`. If no key will be issued, write that in access.md: Rock then stays browser automation (the plan's fallback), and steps 1.8–1.9 need rewriting around the legacy browser code; ask Claude to write that version. |
| **M2** | Before 1.8 | **Rock drafts target (D5).** Ask the admin for (a) a staging Rock server, or (b) test copies of the five channels that nothing sends from and the app doesn't show, plus a drafts Wistia folder that Rock's media sync also covers (so a drafts Spotlight can pick its video). Never the live channels. Record the choice and its ids in access.md. If neither is possible, Rock runs in plan form during drafts runs (D5), and its first real items are made at the switch. |
| **M3** | Start now; before 1.7 | **Vista Social API add-on.** Get the price, and ask support: does the API publish video to Instagram Reels and Facebook; is media uploaded or passed as a public URL; can posts be saved as drafts; what is the schedule time format. Decide yes or no and record it in access.md. If yes, create the key: `dld secrets set vista.api_key`. |
| **M4** | Start now; before 1.6 | **Wistia.** The Account Owner creates an API token with upload and read permission. Note the "Daily Life App" project's hashed id (from its URL) in access.md. Create a project for drafts (e.g. "DLD rebuild test"), ask the Rock admin to include it in Rock's media sync (M2), and note its id too. Store the token: `dld secrets set wistia.token`. |
| **M5** | Before 1.6 | **YouTube OAuth client (D9).** In the Google Cloud project the old app uses (find it from the legacy client id): check the OAuth consent screen is "In production"; add an OAuth client of type "Desktop app" and download its JSON; `dld secrets set youtube.client --from-file <json>`, then delete the downloaded file. Don't create a new project. Then `dld connect youtube` and sign in as the channel owner. |
| **M6** | Before 1.5 | **Claude API key.** console.anthropic.com → API key (pay as you go; title suggestions cost cents a month) → `dld secrets set anthropic.api_key`. Alternatively, set `titles.endpoint` in the series to a local OpenAI-compatible server. |
| **M7** | Before 1.9 | **Unsplash and Pexels keys.** Copy them from the old app's settings, or create new free keys: `dld secrets set unsplash.access_key` and `dld secrets set pexels.api_key`. |
| **M8** | Start now; before 2.5 | **Record a Spotify upload.** First sign in once in a browser profile kept outside the repo: `npx playwright open --channel chrome --user-data-dir ~/DLD-test/spotify-profile https://creators.spotify.com` (if Google sign-in refuses, sign in by email). On a real day, run `npx playwright codegen --channel chrome --user-data-dir ~/DLD-test/spotify-profile https://creators.spotify.com`, upload the vertical video and save it as a draft. Check the generated script for anything personal (emails, ids, typed passwords) and remove it, then put it with short notes on what you check on each screen in `docs/rebuild/spotify/`. Note the show's URL and the publish time you want in access.md (the URL only if it holds no private id). |
| **M9** | Before 1.1 | **Mac toolchain.** Node LTS, `brew install gh` (ffmpeg optional, for test media), Google Chrome, Claude Code; `gh auth login`. |
| **M10** | Before 1.3 | **Real inputs, locally.** Copy the spreadsheet and one or two weeks of media (videos and email thumbnails) into a folder outside the repo, e.g. `~/DLD-test/`. Put the path (only the path) in access.md. Keep the real OneDrive path in mind for 2.1. |
| **M11** | Before 3.2 | **Apple Developer ID** ($99 a year). Enroll; create a "Developer ID Application" certificate and export it as .p12; create an App Store Connect API key (or an app-specific password) for notarization; add them as GitHub Actions secrets with the names step 3.2's log gives. Needed for Gatekeeper and for auto-update, which requires a signed app on macOS. |
| **M12** | Before 3.3 | **Legacy data copy.** Copy the old app's SQLite database and its `credits_*.txt` files off the server into a folder outside the repo (it also holds encrypted secrets: keep it private, delete it after 3.3). Path in access.md. |
| **M13** | After 3.3 | **Two weeks side by side, then the switch (G3).** Follow `docs/rebuild/cutover.md`: approvals and ship (drafts) each day, `dld compare`, mark G3 passed after 14 clean days in a row. Then switch day: untick the old app's platforms, tell staff to stop manual Wistia uploads, switch the new app to live. Mark the first live week clean in the tracker when it is. |
| **M14** | After 3.4 | **Turn off the old app.** Take a final backup of its database and keep it encrypted and offline; stop the `dld` container on dropshippa; remove its tunnel and DNS route; shut down the VPS only if nothing else runs on it. Revoke the server's YouTube refresh token (Google account → third-party access) and sign its stored Rock and Vista sessions out, or change those passwords. |

---

## Part 5 — Reference

Checked 2026-10-14. Rule 1.3.2 applies: if the installed package, the live docs or the server disagree, they win; update this part.

### §A — What the legacy app does

Read from the code at commit `521d822` (`main`, 2026-06-09). Paths are relative to the repo root.

**A1 Filename dates** — `core/file_scanner.py`, `parse_names` (L243–276).

- Extensions:
  - video `.mp4 .mov .avi .mkv .webm`;
  - audio `.mp3 .m4a .wav .aac .ogg`;
  - images `.jpg .jpeg .png .webp` (L35–37).
- Non-digits are stripped, and the digit count picks the rule (`_parse_date_entry_from_stem`, L190–237):
  - 3 digits: M-DD (`602` = June 2), a sixth format the docs don't mention;
  - 4 digits: MMDD;
  - 6 digits: YYMMDD and DDMMYY;
  - 8 digits: DDMMYYYY, then YYYYMMDD (no ambiguity check);
  - longer names: every 8-digit window first, then 6-digit windows.
- Validation: years 1990–2099, two-digit years mean 20YY; Python's `date()` rejects impossible days, which the TypeScript port must do explicitly.
- MMDD and M-DD take the current year, or next year when the date would be more than 60 days in the past.
- Ambiguous 6-digit names:
  - both readings must be plausible (year > this year − 5); YYMMDD is the primary;
  - the file is listed under both dates;
  - the scan keeps only dates with a sheet row (`blueprints/media.py:672–680`).
- Tests: `tests/test_file_scanner_dates.py` (10) and `tests/test_file_scanner_names.py` (6) depend on today's date. Pin the clock when porting.

**A2 Spreadsheet** — `core/excel_parser.py`, `parse_spreadsheet` (L138).

- .xlsx only, read with openpyxl `read_only`, `data_only`.
- Headers are normalized the pandas way: blanks become `Unnamed: N`, duplicates get `.1`.
- Date cells: native dates first, then in order `%Y-%m-%d`, `%m/%d/%Y`, `%d/%m/%Y`, `%B %d, %Y`, `%b %d, %Y`, `%m/%d/%y`, `%d/%m/%y`, then 6-digit YYMMDD.
- When a date repeats, the last row wins, with a warning.
- Tags are split on commas.
- The mapping (14 keys):

| Key | Fills | Fallback / note |
|---|---|---|
| `sheet_name` | worksheet | — |
| `date_column` | join key | required |
| `youtube_title_column` | horizontal video title (dropped) | fallback for the Shorts, podcast and episode titles |
| `shorts_title_column` | YouTube Short title | YouTube title, then filename; the UI asks the LLM when blank |
| `podcast_title_column` | SimpleCast title (dropped) | YouTube title |
| `episode_title_column` | **Rock Spotlight title**; Spotify title in the new app (plan Q&A) | YouTube title. The legacy UI tooltip wrongly says SimpleCast |
| `description_column` | YouTube description, podcast summary and notes, Rock Email body (email and SMS), Vista fallback | Rock Daily Experience does not use it |
| `vista_caption_column` | Vista Social caption | description |
| `tags_column` | YouTube tags | — |
| `passage_column` | Rock Vista title; reference appended to the verse | — |
| `scripture_column` | Rock Vista content; verse for the photo keywords | — |
| `prayer_column` | Rock Reflection content | — |
| `topic_column` | extra hint for the photo keywords | — |
| `transcript_column` | title suggestions | — |

**A3 YouTube** — `uploaders/youtube_uploader.py`, `upload_video` (L431).

- Upload:
  - resumable, 5 MB chunks;
  - retries on 500/502/503/504/408/429 and network errors, 5 tries, `2**attempt` + jitter;
  - httplib2 timeout 120 s, with 308 removed from its redirect codes (the second chunk fails otherwise).
- Request body:
  - `publishAt` = UTC `%Y-%m-%dT%H:%M:%S.0Z`, privacy forced to `private`;
  - `categoryId` 22, `madeForKids` false, no playlist.
- Description (L518–545):
  1. Start with the description.
  2. For Shorts, `f"{description}\n#Shorts".strip()` if `#Shorts` is absent.
  3. Then `"\n\n" + footer.strip()`, only when the footer isn't blank.

  Tags come from the tags column.
- Watch URL: `https://www.youtube.com/shorts/{id}` (L649). Rock Email stores this exact form.
- OAuth: scope `https://www.googleapis.com/auth/youtube`. The legacy hosted flow is web + PKCE; the desktop flow ran only on the main thread.

**A4 Publish times** (America/New_York, `config.yaml`):

| Destination | Time |
|---|---|
| YouTube Shorts | 14:00 |
| Vista Social | 07:00 |
| SimpleCast (seed for Spotify until M8 says otherwise) | 00:00 |
| YouTube horizontal video | 03:00 (dropped) |

Notes:

- Rock items publish on save, and Rock Email's start is the date.
- SimpleCast rounded its whole datetime to 5 minutes.

**A5 Rock** — `uploaders/rock/`. Everything is browser automation today; there is no REST code.

- Daily Experience (`orchestrator.py:39`):
  - checks the required fields;
  - gathers the photo;
  - skips if a parent exists for the date;
  - creates Reflection → Vista → Spotlight, then the Parent;
  - links the children to the parent;
  - records the photo and its credit.

  Items publish on save. Reflection and Vista content is Rock's structured (EditorJS) content (`client.py:547`).

  | Item | Title | Fields |
  |---|---|---|
  | Parent | `Daily Life {Month} {d}` (no year) | active date = the day; Enable Prayer = false; Prayer Count = 3 |
  | Reflection | `{Mon} {d}` ("Dec 25") | Content = prayer; Public Title = "Pause & Pray"; Prompt Title = "Personal Reflection"; Prompt = "What is your takeaway for today?" |
  | Vista | the passage | Content = scripture with whitespace collapsed and `" – {passage}"` appended if missing (dash variants count as equal, `text.py:30–74`); Tagline = "Verse"; Background Image = the photo; Has Likes Enabled = true |
  | Spotlight | episode title | Tagline = "Watch"; orientation "Vertical"; media account "Wistia" → folder "Daily Life App" → element `app YYMMDD` (the `ddlMediaAccount` / `ddlMediaFolder` / `ddlMediaElement` pickers, `client.py:297–301`) |

- Ids (`constants.py`):
  - base `https://rock.lcbcchurch.com`;
  - channel GUIDs: parent `5a2ece2c-88b0-47eb-bea0-cbdb50e5c86c`, Reflection `5a961ea6-2f47-43c4-9190-8d21fe7c5898`, Spotlight `3199ba17-c18a-4a0e-82d6-6ff2160c145c`, Vista `3c569f1a-d6c0-485f-994e-d993dc5df641`, Email `2182c1f3-8f8c-44f3-987f-75a698fe44a7`;
  - child channel ids: Reflection 124, Spotlight 125, Vista 126; email channel id 24.
- `app YYMMDD` = the first standalone 6-digit run in the Shorts filename (`core/session_state.py:21–37`, `infer_wistia_ref`).
- Email / SMS (`email.py`, `schedule_email`; `client.py:370–415`):
  - title `Daily Life {Month} {d}, {YYYY}`; start = the date;
  - Email Message = `description + "\n\n" + ` the Email Message box's pre-filled text (production: "Here is today's Daily Life:");
  - SMS = `description + "\n\n" + ` the SMS box's own pre-filled text;
  - YouTube Link = the watch URL; Thumbnail = the email thumbnail;
  - Sent stays "No". The code calls the item "queued, not sent"; whether Rock then sends it on its start date is M1's question;
  - fails without a YouTube URL; skips when an item with the same title exists.
  - Legacy form field ids: Email Message `59105`, SMS `59102`, YouTube Link `59649`, Thumbnail `58168`, Sent `58362`. These are form controls, not REST attribute keys.
- Tests worth porting: `tests/test_rock_helpers.py` (13), `tests/test_rock_email.py` (9).

**A6 Vista Social** — `uploaders/vista_social_uploader.py`, `upload_post` (L707), browser.

- Profiles: forces Facebook and Instagram on and YouTube off, and verifies it.
- Caption: `(vista_caption or description).strip() + "\n\n" + footer`.
- Video: attaches the Shorts video and waits up to 15 min for "Attached videos".
- Schedule (always scheduled, never immediate):
  - converted to New York time;
  - date typed as "Apr 29, 2026";
  - dropdowns: hours "01"–"12", minutes "00"–"59", AM/PM.
- Returns the calendar URL, with no post id and no already-posted check.

**A7 SimpleCast** (the pattern for Spotify) — `uploaders/simplecast_uploader.py`, `upload_episode` (L487).

- Title: podcast title, else YouTube title.
- Summary and notes: `description + "\n\n" + footer`; the podcast footer is empty.
- Waits up to 10 min for the Save button.
- Schedule: rounded to 5 minutes; hours "00"–"11" + am/pm.
- If scheduling fails, the episode stays a draft (`needs_manual`).
- Tests: `tests/test_simplecast_schedule_math.py`.

**A8 Titles** — `core/llm_title_gen.py`; prompt at L174–198.

- Shorts style, under 60 characters, returns a JSON array.
- Source: the transcript column only. It is cut to 2000 characters at prompt-build time, and the cache key hashes the full text (L76–85). There is no ASR anywhere.
- Count: 3 in config, 5 in the code default; the plan wants 5.
- Settings: temperature 0.8; cached for 24 h.
- UI: only the Shorts tab asks for suggestions, and the first fills in automatically.

**A9 Circuit breaker** — `core/circuit_breaker.py`.

- Closed / open / half-open; 3 failures; 120 s; one trial call in half-open.
- Counts only session-expired, timeout, connection and OS errors (`core/upload_jobs.py:37–43`); data errors and skips don't. Only a real success resets it.
- One breaker per platform, plus `llm:title`, `image:unsplash` and `image:pexels`.

**A10 Stock photos** — `core/image_gatherer.py`, `gather_image_for_verse` (L119).

- Keywords: the LLM gives 3 nature or landscape terms (no religious words, people or books); temperature 0.5.
- Search:
  - Unsplash first: `content_filter=high`, portrait, 10 per page, the download ping, `urls.full` falling back to `regular`;
  - then Pexels: portrait, `large2x` → `large` → `original`.
- Limits: downloads capped at 25 MB; no photo within 60 days, no topic within 14 (`image_history`).
- Credits: `credits_<YYYY-MM>.txt`, tab-separated: `date source photographer topic url`.

**A11 Waits** — `core/upload_jobs.py`.

- Rock Email polls every 1 s for the same date's YouTube result, capped at 7200 s (`upload.youtube_wait_timeout_seconds`).
- A YouTube failure releases the wait at once.
- Emails are submitted last.

**A12 Quota and idempotency** — `core/quota.py`.

- Costs:
  - upload 100; thumbnail 50;
  - list calls 1; search.list 100.
- Cap 10,000; the day rolls over in America/Los_Angeles; units are charged after success. The preflight estimate assumes 150 per upload.
- Idempotency: the legacy skip is keyed on a process session id, so it doesn't survive restarts. Only the Rock flows check the platform: Daily Experience by date, Email by title.

**A13 Calendar refresh** — `core/calendar_refresh.py`.

- Manual trigger; the window is 30 days back and 180 forward.
- YouTube is read through the API: uploads playlist, search, videos.list. A Short is detected by a HEAD request to `/shorts/<id>`, else by duration ≤ 60 s.
- The other platforms are scraped.

**A14 Preflight** — `blueprints/preflight.py`.

- Blocking: a YouTube token, and a saved login present for each browser platform (validity isn't tested).
- Non-blocking: an image key and the LLM being reachable.
- Data checks per date and platform: the media matched; titles present; the Rock fields present; a Shorts filename with a 6-digit code; Rock Email needs YouTube in the same run.
- A quota estimate.

**A15 Docs vs code** (the code is right):

| Topic | CLAUDE.md / README / UI say | The code does |
|---|---|---|
| Email wait | 30 min | 7200 s |
| Quota storage and day | `QUOTA_COSTS` in app.py, kept in the Flask session | `core/quota.py`, SQLite, Los Angeles day |
| Date formats | five | six (3-digit M-DD too) |
| `episode_title` | SimpleCast title (UI tooltip) | Rock Spotlight title |
| `description` | used by Rock Daily Experience (tooltip) | not used there |
| Unused settings | `sc_thumbnail`, `simplecast.default_season` / `explicit`, `llm.model` | never read |

### §B — Platform facts to confirm

From the plan's sources, which were read through search summaries only (plan "Sources"). Confirm each before building on it (rule 2), and replace "to confirm" with what you found and the date.

| Service | Fact (to confirm) | Source |
|---|---|---|
| YouTube | Resumable upload protocol for `videos.insert` (the Node googleapis client doesn't implement it; D3); `status.publishAt` needs `privacyStatus: private`; Shorts = vertical, ≤ 3 min; upload cost reportedly 100 units since Dec 4, 2025 (was 1,600); default 10,000 units/day; unaudited projects created after July 28, 2020 upload private-only | [Quota costs](https://developers.google.com/youtube/v3/determine_quota_cost), [ChannelCrawler](https://channelcrawler.com/insights/youtube-api-daily-limit-quotas-costs-and-how-to-scale-beyond-10000-units-channelcrawler), [Shorts length](https://async.com/blog/how-long-can-youtube-shorts-be/) |
| Wistia | `POST https://upload.wistia.com` with a Bearer token, `project_id`, `name`; only the Account Owner can create tokens | [Upload API](https://docs.wistia.com/docs/wistia-upload-api) |
| Vista Social | `POST /v2/posts/save` with `x-api-key`; the API is a paid add-on; video attach method, drafts and the time format unknown (M3) | [Support article](https://support.vistasocial.com/hc/en-us/articles/32993061787035-Vista-Social-API), [API reference](https://apidocs.vistasocial.com/) |
| Rock RMS | REST with a REST key in the `Authorization-Token` header; `ContentChannelItems` (with `StructuredContent`), attribute values, `ContentChannelItemAssociations`, binary file upload; v1 vs v2 depends on the Rock version | [The Rock REST API](https://community.rockrms.com/developer/303---blast-off/the-rock-rest-api), [attribute Q&A](https://www.rockrms.com/ask/developing/1217) |
| Rock media sync | The Spotlight pickers (account → folder → element) look like Rock's built-in media elements, synced from a Wistia media account by a Rock job; the plan cited Triumph Tech's plugin. Which one, how often it runs, whether REST can trigger it | [Wistia Media Sync](https://triumph.tech/documentation/wistia-media-sync), the server (1.9) |
| Spotify for Creators | no public upload API; video episodes upload in the web app; vertical video displays correctly (checked on a live episode, plan Q&A) | [Publishing videos](https://support.spotify.com/us/creators/article/publishing-videos/), [Video specs](https://support.spotify.com/us/creators/article/video-specs/) |
| Apple / updates | Developer ID signing and notarization for distribution outside the App Store; electron-updater on macOS needs a signed app; @electron/universal needs `x64ArchFiles` for files identical in both architectures | electron-builder and electron-updater docs |
| Claude API | `@anthropic-ai/sdk`; structured output; model ids per the claude-api skill (default `claude-opus-5-5`, checked 2026-10-14) | claude-api skill |

Hosts for `dld doctor`: `www.googleapis.com`, `upload.wistia.com`, `api.wistia.com`, `rock.lcbcchurch.com`, `api.vistasocial.com` (confirm), `creators.spotify.com`, `api.anthropic.com`, `api.unsplash.com`, `api.pexels.com`.

### §C — Secret names (keychain, §S4)

| Name | What | From |
|---|---|---|
| `youtube.client` | Desktop OAuth client JSON | M5 |
| `youtube.token` | refresh token | `dld connect youtube` |
| `wistia.token` | API token (upload + read) | M4 |
| `vista.api_key` | Vista Social API key | M3 |
| `rock.rest_key` | Rock REST key | M1 |
| `anthropic.api_key` | Claude API key | M6 |
| `unsplash.access_key`, `pexels.api_key` | stock photos | M7 |

---

## Part 6 — Coverage matrix

Every element of the plan and the step that builds it. `docs/rebuild/build-tracker.md` tracks status.

| Plan element | Step(s) |
|---|---|
| Lead: one desktop app; retire web app, relay, VPS | 2.1, 3.2, 3.4, M14 |
| At a glance: 3 inputs, 6 destinations, 2 waits | 1.3 (inputs), 1.4 (waits), 1.6–1.9, 2.5 |
| Platform: YouTube Short | 1.6, M5 |
| Platform: Spotify | 2.5, M8 |
| Platform: Wistia | 1.6, M4 |
| Platform: Vista Social | 1.7 (API) or 2.4 (browser), M3 |
| Platform: Rock Daily Experience | 1.9, M1, M2 |
| Platform: Rock Email / SMS | 1.8, M1, M2 |
| Desktop vs web: files read in place, logins on the user's screen | 1.3, 2.4 |
| Desktop vs web: one person, one Mac; spreadsheet on disk | D2, 1.3 |
| Architecture: UI, engine, SQLite, keychain, browser runner | 2.1 (§S14), 1.2 (§S3, §S4), 2.4 (§S11) |
| Architecture: series / day / task model and states | 1.2 |
| Architecture: connectors declare needs and produces | 1.4 |
| Tech stack: Electron + TypeScript, utility process | D3, 1.1, 2.1 |
| Tech stack: playwright-core + Chrome, one profile each | 2.4 |
| Tech stack: googleapis, HTTPS clients | D3 (google-auth-library + §S6), 1.4, 1.6–1.9 |
| Tech stack: SQLite + migrations, keychain via safeStorage | 1.2 (D8: keychain directly) |
| Tech stack: bundled ffprobe media checks | 1.3 (D3: mp4box.js instead) |
| Tech stack: Claude titles, optional local endpoint | 1.5 |
| Tech stack: Vitest, Playwright UI tests | all; 2.1 onward |
| Workflow: set up once | 1.2, 2.1 |
| Workflow: watch the week fill in | 1.3, 2.2 |
| Workflow: approve titles | 1.5, 2.2 |
| Workflow: preview | 1.5, 2.2 |
| Workflow: preflight | 1.4, 2.3 |
| Workflow: ship (order, lanes, retries, breaker) | 1.4, 2.3 |
| Workflow: verify | 1.4 (per connector), 3.1 |
| Workflow: hear about it | 2.3 (OS); Later (email, Slack) |
| New: edit once, update everywhere | Later |
| New: pull a day | 1.4 (drafts), 3.1 (live, unschedule) |
| New: login health | 2.4 |
| New: assisted mode | 2.4, 2.5 |
| New: failure evidence | 2.4 |
| New: status write-back | Later |
| New: station mode | Later |
| New: templates with live preview, series export / import | 1.5, 2.1 (import / export); Later (editor) |
| Keep: filename dates, breaker, stock photos + credits, YouTube Shorts, Rock text rules | 1.3, 1.4, 1.9, 1.6, 1.5 |
| Rework: column mapping | 1.2, 1.3 |
| Rework: titles | 1.5 |
| Rework: preflight | 2.3 |
| Rework: email waits | 1.4 |
| Rework: idempotency | 1.4 (D11) |
| Rework: calendar refresh | 3.1 |
| Rework: Rock REST | 1.8, 1.9 |
| Rework: Rock browser fallback | D1: only if M1 brings no key; otherwise assisted mode (2.4) |
| Rework: Vista API | 1.7 |
| Rework: keychain | 1.2 |
| Rework: updater | 3.2 |
| Add: Wistia, Spotify | 1.6, 2.5 |
| Drop: SimpleCast, horizontal video, web app, relay, multi-tenant, VNC/VPS, llamafile | 3.4, M14 |
| Roadmap gates G0–G3 | Phase 0, 1.10, 2.6, M13 |
| Risk: no Rock REST key | M1 |
| Risk: Wistia sync lag | D10, 1.9, 3.1 |
| Risk: Vista add-on | M3, 2.4 |
| Risk: quota | D9, 1.6 |
| Risk: Shorts classification | 1.3, 2.3 |
| Risk: distribution | 3.2, M11 |
| Risk: rewrite risk | 3.3, M13 |
| Q&A: Mac only, one person, OneDrive sheet, Spotify hosting and title, other churches | D2, 1.3, 2.5, Later |
