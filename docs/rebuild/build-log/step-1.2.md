# Step 1.2 — Series config, store, task states, secrets

2026-10-14 · [PR #118](https://github.com/G0Osey99/DailyLifeDistributor/pull/118) · plan "Architecture", "Keep, drop, add"; guide §S2, §S3, §S4, D3, D5, D6, D8, D10, D11

## Done

- **SQLite driver (D3): `node:sqlite`.** `desktop/scripts/sqlite-runtime-check.mjs` (`npm --prefix desktop run sqlite:check`) opens a file database with WAL, foreign keys, a busy timeout, a rolled-back transaction and `json_extract`, under both runtimes. Neither needs a flag, and neither prints an ExperimentalWarning:

  ```
  {"runtime":"node","node":"24.18.0","modules":"137","sqlite":"3.53.1","execArgv":[],"journal":"wal","fkEnforced":true,"rolledBack":true,"json":2}
  {"runtime":"electron 44.5.1","node":"24.21.0","modules":"149","sqlite":"3.53.4","execArgv":[],"journal":"wal","fkEnforced":true,"rolledBack":true,"json":2}
  ```

  The second line ran under `ELECTRON_RUN_AS_NODE=1 electron`, with Electron 44.5.1 as an exact dev dependency. The whole Vitest suite also passes under Electron's Node (`ELECTRON_RUN_AS_NODE=1 ./node_modules/.bin/electron node_modules/vitest/vitest.mjs run`: 167 passed).
- **§S2, `src/engine/config.ts`.**
  - `SeriesSchema`: zod strict objects all the way down. `parseSeries` and `parseSeriesJson` throw `SeriesError`, with one `{path, message}` per problem.
    - An unknown key is reported at its own full path, with a "did you mean" suggestion. This works inside arrays and inside the `"TODO" | "plan" | {…}` unions.
    - A missing key is reported as `missing (expected …)`.
  - `TODO` is a valid placeholder wherever a value isn't known yet. `findTodos` lists their paths.
  - `draftsClashes` is the D5 check:
    - Wistia: the drafts project must differ from the live one, by id and by name (the name is the folder Rock's Spotlight picker uses).
    - Rock: every drafts channel is compared with every live channel of both Rock destinations, on the same server, by GUID and by id. A staging server with the same GUIDs passes; the same server with the same channel fails.
  - `checkSeries` runs the schema, then D5, then the TODO list. `formatSeries` prints 2-space JSON in schema order. `seriesHosts` lists the Rock hosts (live and drafts) for doctor.
  - Tests: `test/config.test.ts`.
- **`desktop/examples/daily-life.series.json`**: today's values from Part 5 §A. It validates, has no D5 clash and 34 `TODO`s.
  - Times: YouTube 14:00, Vista 07:00.
  - Footers: Shorts and Vista "Share this daily devotional…"; podcast (Spotify) "".
  - Rock base URL, the five channel GUIDs, and ids 124/125/126/24. The Rock text constants: "Pause & Pray", "Personal Reflection", the prompt, "Verse", "Watch". The Rock settings: Enable Prayer false, Prayer Count 3, likes on, "Vertical", media account "Wistia".
  - Wistia project "Daily Life App" and media "app YYMMDD". Vista profiles facebook + instagram. YouTube category 22.
  - Limits: quota costs and cap 10000; email wait cap 7200 s; breaker 3 failures / 120 s.
  - Biome formats `examples/**/*.json` with `expand: always`, so the file is byte-identical to `dld series export`.
- **§S3, `src/engine/db/`.**
  - `db.ts`: the `Db` class sets WAL, `foreign_keys`, `busy_timeout` 5000 and an injectable clock and process. It has `one`/`all`/`run` and `tx()` (`BEGIN IMMEDIATE`, savepoints when nested, sync only). It also has `loadMigrations`, `migrationStatus` and `migrate`.
    - Migrations are `NNN_name.sql` with a sha256. Gaps, an edited applied migration and a database newer than the build are all refused.
  - `migrations/001_init.sql` creates `series`, `days`, `tasks`, `events`, `quota`, `photos`, `settings` and `locks`.
    - Days are unique on `(series_id, date)` and tasks on `(day_id, destination)`. `tasks(state)` is indexed.
    - JSON columns are checked with `json_valid`, and state and mode are checked against their lists.
  - `states.ts`: `TASK_STATES`, `NEXT_STATES` (exactly §S3), `transition(db, taskId, to, {reason, data})` and `IllegalTransition`.
    - The UPDATE and the `events` row are written in one transaction.
    - `attempts` counts entries to running. `started_at` and `finished_at` are stamped. `error` is set on failed or needs_login and cleared on scheduled or verified.
    - `createTask` records the mode and a `created` event.
  - `locks.ts`: the writer lock (pid, host, purpose, heartbeat) and `acquireLock`, which throws `LockHeld` naming the holder. Also `heartbeat` (returns false once broken), `releaseLock` and `withLock` (beats every 30 s).
    - A lock is broken when its heartbeat is older than 2 minutes, or at once when its pid is gone on this Mac.
    - `recoverOrphans`: running tasks go to failed ("interrupted"), then to ready.
  - `index.ts`: `open(path, {migrate?, recover?})` runs crash recovery at open on a migrated database. `openStore()` is the data dir's `dld.db`, migrated. `dbPath()`.
  - `repo.ts`: `saveSeries` (upsert; the first series becomes the default), `getSeries` (validates again), `listSeries`, `resolveSeriesId`, `upsertDay`/`getDay`, `listEvents`, `getSetting`/`setSetting`, `tableCounts`, `taskCountsByState`.
  - Tests: `test/db.test.ts`, `test/states.test.ts` (all 18 allowed moves and all 46 illegal ones), `test/locks.test.ts`.
- **§S4, `src/engine/secrets.ts`.**
  - The `Secrets` interface (`get`/`set`/`delete`/`has`) has two implementations:
    - `KeychainSecrets` over `/usr/bin/security`, service `daily-life-distributor`.
    - `MemorySecrets` for tests.
  - `KNOWN_SECRETS` is Part 5 §C, with each secret's fix.
  - `set` and `delete` send `add-generic-password -U … -X <hex>` / `delete-generic-password …` to `security -i` on stdin. argv is only `['-i']`.
  - `get` uses `find-generic-password … -g` and `parsePasswordLine`. `has` reads attributes only.
  - Every value read or written goes through `registerSecret`. Names are checked so they are safe tokens.
  - Tests: `test/secrets.test.ts`. `node:child_process.spawn` is mocked with `test/fakes/security.ts`, a fake `security` that reproduces the real tool's output forms, its exit 44 and its 4095-character line split. `test/keychain.integration.test.ts` runs against the real keychain with `DLD_INTEGRATION=1`.
- **CLI** (`src/cli/series.ts`, `secrets.ts`, `db.ts`, `prompt.ts`, `format.ts`; registered in `program.ts`):
  - `dld series import <file> | export [id] [file] [--force] | show [id] | check <file>`.
  - `dld secrets set <name> [--from-file <path>] | list | delete <name>`. Input is read at a hidden prompt (raw mode, no echo) or from piped stdin. `youtube.client` must be JSON.
  - `dld db migrate | status | backup <path>`.
  - `runCli` now prints any action error as `dld: <redacted message>` and returns 1.
  - Tests: `test/cli.test.ts`.
- **Doctor**: one row per §C secret (warn when missing, never a fail; `hasSecret` never reads the value) and a `series` row. Rock's reach row now comes from the stored series' base URLs, live and drafts, not from code. This closes step 1.1's rule-11 gap. Tests: `test/doctor.test.ts` (regression: same rows plus the new ones; the Rock row moved from 4th host to last).
- Scripts: `test:integration` (`DLD_INTEGRATION=1 vitest run`) and `sqlite:check`.

## Decisions made

- **`node:sqlite`, not better-sqlite3.** Both runtimes expose it with no flag (evidence above). There is no native module, no install script to approve under npm 11, and nothing to rebuild for Electron's ABI 149 or for the universal build in 3.2.
- **Electron 44.5.1** is pinned as a dev dependency, for 2.1: it is the newest release at least two weeks old (published 2026-09-30; 44.7.2 is a day old). Its package has no install script. The binary downloads on first use, so `npm ci` in CI doesn't fetch it. Its new transitive deps are all MIT, ISC, BSD-2-Clause or Apache-2.0, and dev-only. The runtime dependencies are unchanged (commander, zod).
- **Reads parse `security find-generic-password -g`, not `-w`.** On this Mac, `-w` prints a printable value raw and any other value as bare hex, so a printable value that looks like hex (`ABCDEF12`) can't be told apart. `-g` prints `password: "text"` or `password: 0xHEX  "…"`, which is unambiguous. Values are still written as the real bytes with `-X` (guide §S4), so Keychain Access shows the real value.
- **`security -i` reads at most 4095 characters per line.** A longer line is split, and its tail runs as a separate command. `set` therefore refuses a value whose command line would pass the limit: about 2000 bytes, 2012 for `youtube.client`. A Desktop OAuth client JSON is about 400 bytes.
- **`security -i` exits with the last command's status**: 44 when the item isn't there. `delete` uses that to report "was not in the keychain" (exit 0, so it is idempotent).
- **A lock is broken at once when its pid is gone on this host**, as well as after 2 minutes without a heartbeat (§S3). A crash then recovers on the next open, not 2 minutes later. A reused pid still goes stale through the heartbeat.
- **Crash recovery at open covers any running task** whenever no live writer holds the lock. That includes running tasks with no lock row at all, because only the writer runs tasks (§S14). It reads the lock inside `BEGIN IMMEDIATE`, so it can't race a writer that is starting.
- **`pulled` is final**, and a self-move (x → x) is illegal: §S3 lists no move out of `pulled`, and "any → pulled" doesn't include pulled itself.
- **`TODO` is a first-class placeholder.** It validates, and `check`, `import` and `show` list every one. A connector that needs one will refuse at preflight (1.4 onwards). TODO values never count as a D5 clash.
- **Unknown values are TODO, not guesses.**
  - The media folder, the sheet path, the sheet name and seven column headers are TODO. The headers "Episode Title", "Passage", "Scripture" and "Prayer" come from the legacy `uploaders/rock/fields.py` comments.
  - The parent channel's integer id is TODO; only its GUID is in the legacy code.
  - `access.md` is still blank, so every M1–M4 and M8 value is TODO.
- **`topic_column` is optional too**, beside youtube_title and podcast_title: the legacy UI says it is "Safe to leave unmapped", and the photo keywords use it only as an extra hint. The other ten columns are required keys (their values may be TODO).
- **All six destinations are enabled** (D1). The legacy `config.yaml` has `rock: false`, `rock_email: false`, but the plan ships all six. A destination whose connector isn't built yet is the runner's concern (1.4).
- **Drafts targets:**
  - YouTube is `"private"` and Spotify is `"draft"`: D5 already decides both.
  - Wistia, Vista and both Rock targets are TODO (M2–M4).
  - A Wistia drafts target is `{projectName, projectId}`, because Rock's Spotlight picks its video by folder name (1.9).
- **The quota cost keys are API method names** (`videos.insert` 100; `videos.list`, `playlistItems.list` and `channels.list` 1; `search.list` 100), not the legacy `shorts_upload` names. Thumbnails are dropped (D1). `videos.update` and `videos.delete` are left for 1.6 to confirm (rule 2). The Los Angeles quota day is a YouTube fact, so it stays in code (1.6), not in the series.
- **Waits** are `youtube.url` {poll 1 s, cap 7200 s} (legacy) and `rock.mediaSynced` {poll 300 s, cap 259 200 s}. The second is a placeholder until M1 says how often Rock's sync runs; 1.9 owns its name and values.
- **Aspect tolerance 0.02** (2 %) around 9:16. The legacy app has no such check; 1.3 may tune it against real files.
- **Times only for destinations that schedule** (YouTube, Vista, Spotify). Wistia uploads at once, Rock items publish on save, and Rock Email's start is the date (Part 5 §A4).
- **Templates hold only fixed text for now**: the footers and the Rock labels. Step 1.5 adds the `{{field}}` templates; template names are free camelCase until then.
- **Byte-exact round trip.** zod outputs keys in schema order, so the example is written in schema order, with no schema defaults. Biome's `expand: always` override on `examples/**/*.json` makes Biome and `JSON.stringify(…, 2)` agree.
- **`series import` refuses a D5 clash and stores nothing.** Re-importing the same id replaces the stored series (an `updated` event).
- **Overwrites are refused.** `series export` to an existing file needs `--force`, and `db backup` never overwrites. Backups use SQLite's online backup API (`node:sqlite` `backup()`), which is consistent while another process writes.
- **There is no `dld secrets get`.** Printing secrets isn't needed for any step; the integration test and the CHECK read values back in code.
- **`dld secrets set` reads piped stdin when it isn't a TTY**, so scripts can store a value without it appearing in argv.

## Gates

- `npm --prefix desktop run check`: Biome clean (37 files), `tsc --noEmit` clean, Vitest **167 passed, 3 skipped** (the opt-in keychain integration tests), 10 files.
- `DLD_INTEGRATION=1` keychain integration test on this Mac: **3 passed**. Each case (JSON with quotes; multi-line non-ASCII JSON with a tab; `ABCDEF12`) is stored, read back, updated in place with `-U`, deleted, then confirmed gone.
- `python3 scripts/check_prompts.py`: all prompts pass (longest 3488 chars).
- `npm --prefix desktop run dld -- doctor` on this Mac (macOS 26.6.2 arm64), exit 0, **no new failures**. The 8 new warnings are the secrets not stored yet (Manual items):

```
STATUS  CHECK                       DETAIL                                                      FIX
ok      macOS                       macOS 26.6.2 (arm64)
ok      Node                        Node 24.18.0
ok      data dir writable           ~/Library/Application Support/Daily Life Distributor
ok      free space                  1375.3 GB free
ok      Google Chrome               Chrome 155.0.8059.40 in /Applications
info    ffprobe (optional)          ffprobe 9.0.2 at /opt/homebrew/opt/ffmpeg-full/bin/ffprobe
ok      keychain tool               /usr/bin/security
warn    secret youtube.client       YouTube Desktop OAuth client (JSON): missing                M5: dld secrets set youtube.client --from-file <client.json>
warn    secret youtube.token        YouTube refresh token: missing                              dld connect youtube (step 1.6, after M5)
warn    secret wistia.token         Wistia API token (upload + read): missing                   M4: dld secrets set wistia.token
warn    secret vista.api_key        Vista Social API key: missing                               M3: dld secrets set vista.api_key
warn    secret rock.rest_key        Rock REST key: missing                                      M1: dld secrets set rock.rest_key
warn    secret anthropic.api_key    Claude API key: missing                                     M6: dld secrets set anthropic.api_key
warn    secret unsplash.access_key  Unsplash access key: missing                                M7: dld secrets set unsplash.access_key
warn    secret pexels.api_key       Pexels API key: missing                                     M7: dld secrets set pexels.api_key
ok      series                      Daily Life (daily-life)
ok      reach www.googleapis.com    YouTube: reachable (264 ms)
ok      reach upload.wistia.com     Wistia upload: reachable (152 ms)
ok      reach api.wistia.com        Wistia: reachable (231 ms)
ok      reach api.vistasocial.com   Vista Social: reachable (281 ms)
ok      reach creators.spotify.com  Spotify for Creators: reachable (459 ms)
ok      reach api.anthropic.com     Claude: reachable (289 ms)
ok      reach api.unsplash.com      Unsplash: reachable (344 ms)
ok      reach api.pexels.com        Pexels: reachable (328 ms)
ok      reach rock.lcbcchurch.com   Rock: reachable (373 ms)

16 ok · 8 warn · 0 fail · 1 info
```

### CHECK on this Mac (real data dir, real keychain)

`dld series import examples/daily-life.series.json` (exit 0). This created `~/Library/Application Support/Daily Life Distributor/dld.db`:

```
Imported series daily-life ("Daily Life").
34 values are still TODO: dld series show daily-life lists them.
```

`dld series show` (exit 0):

```
Daily Life (daily-life) · default · America/New_York

media   TODO
        videos .mp4 .mov .avi .mkv .webm · thumbnails .jpg .jpeg .png .webp
sheet   TODO · sheet TODO
        date_column = TODO
        shorts_title_column = TODO
        episode_title_column = "Episode Title"
        description_column = TODO
        vista_caption_column = TODO
        tags_column = TODO
        passage_column = "Passage"
        scripture_column = "Scripture"
        prayer_column = "Prayer"
        topic_column = TODO
        transcript_column = TODO
titles  claude-opus-5-5, 5 suggestions

DESTINATION  ON   TIME (NY)  DRAFTS   SETTINGS
youtube      yes  14:00      private  category 22
wistia       yes  —          TODO     project "Daily Life App" (TODO), media "app YYMMDD"
vista        yes  07:00      TODO     profiles facebook, instagram
spotify      yes  TODO       draft    show TODO
rock-daily   yes  —          TODO     rock.lcbcchurch.com, channels parent TODO · reflection 124 · vista 126 · spotlight 125
rock-email   yes  —          TODO     rock.lcbcchurch.com, channel 24

waits      youtube.url every 1 s, cap 7200 s · rock.mediaSynced every 300 s, cap 259200 s
limits     Shorts ≤ 180 s · aspect 9:16 ± 2% · breaker 3 failures / 120 s
quota      YouTube 10000 units a day; videos.insert 100, videos.list 1, playlistItems.list 1, channels.list 1, search.list 100
templates  youtube: footer · vista: footer · spotify: footer · rock-daily: reflectionPublicTitle, reflectionPromptTitle, reflectionPrompt, vistaTagline, spotlightTagline
34 values are still TODO:
  media.folder
  sheet.path
  sheet.sheetName
  sheet.columns.date_column
  sheet.columns.shorts_title_column
  sheet.columns.description_column
  sheet.columns.vista_caption_column
  sheet.columns.tags_column
  sheet.columns.topic_column
  sheet.columns.transcript_column
  times.spotify
  destinations.wistia.projectId
  destinations.spotify.showUrl
  destinations.rock-daily.channels.parent.id
  destinations.rock-daily.attributeKeys.parent.enablePrayer
  destinations.rock-daily.attributeKeys.parent.prayerCount
  destinations.rock-daily.attributeKeys.reflection.publicTitle
  destinations.rock-daily.attributeKeys.reflection.promptTitle
  destinations.rock-daily.attributeKeys.reflection.prompt
  destinations.rock-daily.attributeKeys.vista.tagline
  destinations.rock-daily.attributeKeys.vista.backgroundImage
  destinations.rock-daily.attributeKeys.vista.hasLikesEnabled
  destinations.rock-daily.attributeKeys.spotlight.tagline
  destinations.rock-daily.attributeKeys.spotlight.videoOrientation
  destinations.rock-daily.attributeKeys.spotlight.mediaElement
  destinations.rock-email.attributeKeys.emailMessage
  destinations.rock-email.attributeKeys.smsMessage
  destinations.rock-email.attributeKeys.youtubeLink
  destinations.rock-email.attributeKeys.thumbnail
  destinations.rock-email.attributeKeys.sent
  drafts.wistia
  drafts.vista
  drafts.rock-daily
  drafts.rock-email
Stored 2026-10-14T22:00:53.169Z.
```

`dld db status` (exit 0):

```
database  ~/Library/Application Support/Daily Life Distributor/dld.db (80.0 KB, wal)
schema    1 of 1: up to date
series    Daily Life (daily-life) · default
rows      series 1 · days 0 · tasks 0 · events 1 · quota 0 · photos 0 · settings 1 · locks 0
tasks     none
writer    free
```

A dummy JSON secret in the real keychain. The value was `{"selftest":true,"quote":"it's \"quoted\"","spaced":"a b  c","note":"dummy value for step 1.2"}`, in a scratch file outside the repo:

```
$ dld secrets set dld.selftest --from-file selftest.json
Stored dld.selftest in the keychain (95 bytes).
The keychain now holds it: you can delete …/scratchpad/selftest.json.
note: dld doesn't read dld.selftest; dld secrets list shows the names it uses.
$ security find-generic-password -s daily-life-distributor -a dld.selftest   (attributes only)
class: "genp"
    "acct"<blob>="dld.selftest"
    "svce"<blob>="daily-life-distributor"
$ read back through KeychainSecrets.get
read back: {"selftest":true,"quote":"it's \"quoted\"","spaced":"a b  c","note":"dummy value for step 1.2"}
identical to the file: true · parses as JSON: true
$ dld secrets delete dld.selftest
Deleted dld.selftest from the keychain.
$ dld secrets delete dld.selftest
dld.selftest was not in the keychain.
item after delete: security exit 44 (44 = not found)
```

`dld secrets list`: all 8 names `missing`, `0 present · 8 missing`, exit 0.

## Manual items

- **Your check** (guide 1.2):
  1. Run `npm --prefix desktop run dld -- secrets list`. Every §C name should be listed as `missing` (or `present` for any key you've already set).
  2. Copy `desktop/examples/daily-life.series.json`, rename one key in the copy (e.g. `categoryId` → `categoryID`), and run `npm --prefix desktop run dld -- series check <copy>`. It should exit 1 and name `destinations.youtube.categoryID: unknown key (did you mean "categoryId"?)`.
  3. Say "merge".
- **Store keys as they arrive.** Use the hidden prompt, or `--from-file` for the YouTube client JSON:
  - M1: `dld secrets set rock.rest_key`
  - M3: `dld secrets set vista.api_key`
  - M4: `dld secrets set wistia.token`
  - M5: `dld secrets set youtube.client --from-file <client.json>`, then delete the JSON
  - M6: `dld secrets set anthropic.api_key`
  - M7: `dld secrets set unsplash.access_key` and `dld secrets set pexels.api_key`

  `dld doctor` turns each row green.
- **Fill in the series as answers arrive.** Edit a copy of the example (or `dld series export daily-life <file>`), replace the TODOs you now know (Wistia project ids M4, the M2 drafts targets, the Spotify show URL and time M8), run `dld series check <file>`, then `dld series import <file>`. The import replaces the stored series. Ids only: never a key.
- The CHECK created `~/Library/Application Support/Daily Life Distributor/dld.db`, holding the example series. It is the real store from now on: `dld db backup <path>` copies it.

## Known gaps

- **`waiting → failed` isn't a §S3 move.** A wait that passes its cap (1.4's test) must go waiting → ready → running → failed, or 1.4 must extend `NEXT_STATES` and record why.
- **Migrations are read from `src/engine/db/migrations/` through `import.meta.url`.** 2.1's electron-vite bundle must copy them, or import them with `?raw`.
- **No helpers yet for approval (1.5), the quota ledger (1.6) or photo history (1.9).** Their tables exist.
- **Rock attribute keys, the parent channel id and every drafts target are TODO** (1.8, 1.9, M1, M2). The D5 check covers them once they are filled in. The connectors' runtime live-channel guard is 1.8's.
- **Still no automated licence check.** It was checked by hand for this step's new packages, which are all dev-only. It should arrive with packaging in 3.2 at the latest.
- **Templates hold fixed text only** (1.5).

## Next step needs

- **Opening the store:** `openStore()` (from `src/engine/db/index.js`) returns the data dir's `dld.db`, migrated and crash-recovered. Close it in a `finally`.
  - `Db.tx(fn)` is synchronous: never `await` inside it.
  - `db.one/all/run` take positional `?` params.
  - Timestamps come from `db.now()`, UTC ISO.
- **Days:** `upsertDay(db, seriesId, date, {files, row})` creates the day or updates those fields; days are unique on `(series_id, date)`. Use `getSeries(db, resolveSeriesId(db))` for the active series.
- **The planner's series fields:**
  - `media.folder`, `media.videoExtensions` / `thumbnailExtensions` (the legacy lists);
  - `sheet.path`, `sheet.sheetName`, and `sheet.columns`, keyed by the legacy names (`date_column`, …; `youtube_title_column`, `podcast_title_column` and `topic_column` optional);
  - `limits.shortsMaxS` and `limits.aspect`;
  - `times` (only YouTube, Vista and Spotify have one).

  Values may be `TODO`: treat a TODO you need as a gap with its path (`findTodos`).
- **For 1.3's CHECK**, export the series, set `media.folder`, `sheet.*` and the column headers to the M10 copy, and import it under a new id. Each series is stored by id; the first imported stays the default (`settings` `series.default`).
- **Tests:**
  - `test/fakes/store.ts` has `seededStore()`, `fakeClock()`, `fakeProcess()` and `exampleSeries()`.
  - For a fresh database per CLI test, use `vi.stubEnv('DLD_DATA_DIR', mkdtemp…)`, as `test/cli.test.ts` does.
  - Inject a failure inside a transaction with a `RAISE(ABORT)` trigger, as `test/states.test.ts` does.
- **Tasks** (1.4):
  - `createTask(db, {dayId, destination, mode, state})` and `transition(db, id, to, {reason, data})`. Wrap a `produced` update and its transition in one `db.tx()`.
  - `withLock(db, purpose, fn)` holds the writer lock with a heartbeat. `acquireLock` throws `LockHeld` with a message naming the holder's purpose, pid and start time.
- **Secrets:** `new KeychainSecrets()` in code, `MemorySecrets` in tests. Every `get` registers the value with `redact()`. Names live in `KNOWN_SECRETS`.
- **Doctor:** add a probe to `defaultProbes(series)`. Hosts that belong to a church come from the series (`seriesHosts`), never from code.
