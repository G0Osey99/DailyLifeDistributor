# Step 1.1 — Desktop scaffold, `dld doctor`, CI

2026-10-14 · [PR #117](https://github.com/G0Osey99/DailyLifeDistributor/pull/117) · plan "Tech stack", "Architecture"; guide §S1, §S5, D2, D3, D8, D12

## Done

- `desktop/` per §S1: `package.json` (private, ESM, `engines.node` `>=24`, scripts `dld`, `check`, `test`), `tsconfig.json` (strict, NodeNext, `noUncheckedIndexedAccess`), `biome.json`, `vitest.config.ts`, `.npmrc` (`save-exact`, `engine-strict`), `package-lock.json`. Runtime deps commander 15.0.0, zod 4.6.5; dev deps typescript 7.0.2, tsx 4.23.15, vitest 5.0.3, @biomejs/biome 2.5.15, @types/node 24.19.2. All exact.
- `src/engine/paths.ts`: `dataDir(env)` (`DLD_DATA_DIR`, else `~/Library/Application Support/Daily Life Distributor`; pure), `ensureDataDir()`, `dataPath(sub, ...parts)` for `logs | profiles | evidence | cache | credits`, created on demand with mode 0700; refuses `..` escapes. Tests: `test/paths.test.ts`.
- `src/engine/log.ts` (§S5): `registerSecret`, `forgetSecrets`, `redact(value)` and `createRunLogger({dir, runId, now})`.
  - `redact` returns a JSON-safe copy. It removes registered secret values (and their URL-encoded form), and the values of `Authorization`, `x-api-key`, `Authorization-Token`, cookie, OAuth token, `client_secret`, `password` and `token` keys in objects, `Headers` and `Map`s at any depth. It also strips the same things inside strings: header lines, `Bearer`/`Basic` credentials, Google `ya29.` and `1//` tokens, token query parameters and form bodies, and secret fields in JSON text. It handles cycles, errors (message, stack, cause), URLs, dates, bigints and binary data.
  - The run logger writes `<data dir>/logs/<runId>.jsonl` (mode 0600), one `{ts, level, run, msg, data}` line per call, redacted and written synchronously.
  - Tests: `test/log.test.ts` (nested objects, headers in every form, token strings, JSON text, errors, cycles, logger lines and file mode).
- `src/engine/doctor.ts`: `Probe` (`check` + `run(sys)`), an injectable `DoctorSystem`, `realSystem()`, `defaultProbes()`, `runDoctor()` (concurrent, ordered; a probe that throws becomes a fail row), `doctorExitCode()`, `formatTable()`.
  - Rows: macOS (fail elsewhere), Node (fail below 24, warn on an odd major), data dir writable (fail), free space (warn < 10 GB), Google Chrome (warn), ffprobe (info only), `/usr/bin/security` (fail), and one `reach <host>` row per Part 5 §B host (warn). Status `info` is new; it never fails the run.
  - Tests: `test/doctor.test.ts` with every probe mocked through `healthyMac()`. All ok exits 0; a missing security binary fails and exits 1. It also covers non-macOS, old or odd Node, an unwritable dir, warn-only runs exiting 0, a crashing probe, and the fix column.
- `src/cli/program.ts`: `runCli(argv, deps)` resolves to the exit code and never calls `process.exit`; `src/cli/index.ts` sets `process.exitCode`. `dld --version` prints `0.1.0`. Tests: `test/doctor.test.ts` (`--version`, unknown command).
- `src/engine/version.ts`: `VERSION` and `MIN_NODE_MAJOR` from `package.json`.
- `test/setup.ts`: every test file gets its own temp `DLD_DATA_DIR`; registered secrets are cleared after each test.
- `.github/workflows/desktop.yml`: on PRs and pushes to `main` touching `desktop/**`, `docs/rebuild/**`, `scripts/check_prompts.py` or the workflow itself. It runs on macos-latest: setup-node 24 with the npm cache, `npm ci`, `npm run check`, a `dld doctor` smoke run on a temp data dir, `python3 scripts/check_prompts.py`, and a gitleaks scan of the diff's commits.
- `.gitignore`: `desktop/node_modules/`, `desktop/out/`, `desktop/dist/`, `desktop/release/` added; nothing removed.
- `CLAUDE.md`: the rebuild note under the title; nothing else changed. `ci.yml` and `release-agent.yml` untouched.

## Decisions made

- **TypeScript 7.0.2** (the native compiler, July 2026): `tsc --noEmit` type-checks the NodeNext project correctly (checked with a deliberate type error).
- **vitest 5.0.3 and zod 4.6.5, not the latest patches**: 5.0.4 and 4.6.7 were two days old. I pinned the newest release that was at least two weeks old.
- **@types/node 24.x**, to match the runtime, not 26.x.
- **`engines.node` `>=24`**: Node 24 is the Active LTS today. Node 26 becomes LTS around Oct 28, 2026 and is accepted. Doctor fails below 24 and warns on odd (non-LTS) majors.
- **lightningcss (MPL-2.0) accepted as a dev-only transitive dependency.** vite 8 requires it, and vite comes with vitest 5 and with electron-vite in 2.1. MPL-2.0 is file-level copyleft (not GPL/AGPL), and the engine neither ships nor modifies it. Every other installed package is MIT, ISC, BSD-3-Clause or Apache-2.0. This deviates from rule 3's list, so it is recorded here.
- **npm 11 skips unapproved install scripts.** esbuild's postinstall is skipped (and in CI, fsevents' install too); tsx and vitest work without them, locally and in CI. Nothing is approved, so no install script runs.
- **Biome config by hand after `biome migrate`.** The migration turned `recommended: true` into `"preset": "none"`, which turns linting off, so I set `"preset": "recommended"`. VCS ignore is off because Biome looks for a `.gitignore` in `desktop/`; the explicit excludes cover it.
- **`dataDir()` is pure; the `ensure*` and `dataPath` functions create.** This lets doctor report an unwritable dir instead of crashing on import. Dirs are owner-only (0700) because they will hold browser logins.
- **`redact` errs toward over-redaction.** Any `Authorization:` line is redacted to its end, as are `key=`, `token=` and `code=` parameters. Secrets shorter than 4 characters are not registered, because replacing them everywhere would shred the log.
- **Host check = any HTTPS answer to a HEAD request within 5 s.** A 4xx still proves DNS, TCP and TLS work. Hosts are checked concurrently.
- **The Rock host is in code for now** (`SERVICE_HOSTS`). This conflicts with rule 11; the series config in 1.2 should supply it.
- **`api.vistasocial.com` answers HTTPS** (308). The docs page is a JavaScript app, so the API base path is left for 1.7 / M3 to confirm (rule 2).
- **gitleaks 8.30.1 binary, pinned and sha256-checked**, instead of gitleaks-action, which needs a licence key for organisation repos and isn't MIT. The scan reads the PR's `base..head` commits, or `before..sha` on push, with `--redact`.
- **A `dld doctor` smoke run in CI.** Only a fail row breaks it, and on a macOS runner every hard requirement holds.
- **The workflow's own path is a trigger**, so edits to it are tested.
- **actions/checkout@v7 and actions/setup-node@v7, by major tag**, as `ci.yml` does.

## Gates

- `npm --prefix desktop run check`: biome clean (14 files), `tsc --noEmit` clean, vitest **33 passed** (3 files, ~90 ms).
- `python3 scripts/check_prompts.py`: all prompts pass (longest 3488 chars).
- gitleaks 8.30.1 on `origin/main..HEAD`: no leaks.
- `npm --prefix desktop run dld -- doctor` on this Mac (M-series, macOS 26.6.2), exit 0:

```
STATUS  CHECK                       DETAIL                                                      FIX
ok      macOS                       macOS 26.6.2 (arm64)
ok      Node                        Node 24.18.0
ok      data dir writable           ~/Library/Application Support/Daily Life Distributor
ok      free space                  1382.1 GB free
ok      Google Chrome               Chrome 155.0.8059.40 in /Applications
info    ffprobe (optional)          ffprobe 9.0.2 at /opt/homebrew/opt/ffmpeg-full/bin/ffprobe
ok      keychain tool               /usr/bin/security
ok      reach www.googleapis.com    YouTube: reachable (321 ms)
ok      reach upload.wistia.com     Wistia upload: reachable (214 ms)
ok      reach api.wistia.com        Wistia: reachable (437 ms)
ok      reach rock.lcbcchurch.com   Rock: reachable (1083 ms)
ok      reach api.vistasocial.com   Vista Social: reachable (349 ms)
ok      reach creators.spotify.com  Spotify for Creators: reachable (610 ms)
ok      reach api.anthropic.com     Claude: reachable (484 ms)
ok      reach api.unsplash.com      Unsplash: reachable (558 ms)
ok      reach api.pexels.com        Pexels: reachable (569 ms)

15 ok · 0 warn · 0 fail · 1 info
```

- CI on PR #117: both green on the first push (`f164734`).
  - `desktop` [run 38640273790](https://github.com/G0Osey99/DailyLifeDistributor/actions/runs/38640273790): success, a 16 s job. Check: 3 files, 33 tests. Doctor on the runner (macOS 26.6.2 arm64, Node 24.20.0, Chrome 152, no ffprobe): `15 ok · 0 warn · 0 fail · 1 info`. Prompts pass. gitleaks: tarball checksum OK, 1 commit scanned, no leaks.
  - `CI` (legacy Python) [run 38640273780](https://github.com/G0Osey99/DailyLifeDistributor/actions/runs/38640273780): success, about 3 min.
  - The commit that adds this log reruns both workflows; the PR shows their result.

## Manual items

- None from doctor: no fail rows on this Mac.
- **Your check** (guide 1.1): run `npm --prefix desktop run dld -- doctor` (after `npm --prefix desktop ci` on a fresh checkout). Confirm there are no `fail` rows, and that PR #117 shows the `desktop` and `CI` workflows green. Then say "merge".
- The doctor run created `~/Library/Application Support/Daily Life Distributor/` (empty, owner-only). This is the real data dir from now on.

## Known gaps

- The Rock host is hard-coded in `SERVICE_HOSTS`. Step 1.2 should take it from the series config's Rock base URL (rule 11).
- No automated licence check. Add one when the first runtime dependency with transitive deps arrives (1.2–1.4), or at the latest with packaging in 3.2.
- Doctor checks only that Chrome is installed. Login health per platform is 2.4's job.
- The run logger isn't used by a command yet; `dld ship` (1.4) is its first user.
- `dld` runs through `npm --prefix desktop run dld --`. There is no installed `dld` binary until packaging (2.1 / 3.2).

## Next step needs

- **Adding a command:** add it in `runCli` (`src/cli/program.ts`) and test it by calling `runCli([...], { io })` with a fake `io`. Never call `process.exit`. Any thrown error is redacted by `src/cli/index.ts`.
- **Adding a doctor row:** append a `Probe` to `defaultProbes()`, and add anything it touches to `DoctorSystem` (and `realSystem()`). In `test/doctor.test.ts`, extend `healthyMac()`, the row-order list and the `15 ok · … · 1 info` summary.
- **§S4 (1.2)** must call `registerSecret()` on every value it reads. `test/setup.ts` already clears the registry after each test and gives each test file a temp `DLD_DATA_DIR`.
- **Imports** use `.js` extensions (NodeNext). tsx runs `src/` directly; `package.json` is imported with `with { type: 'json' }`.
- **npm 11 and install scripts:** npm skips install scripts unless approved (`npm approve-scripts <pkg>`). better-sqlite3 needs its native build script; `node:sqlite` needs none. Weigh this in 1.2's driver decision (D3).
- **Formatting:** Biome uses single quotes, no semicolons, trailing commas and 100 columns. `npx biome check --write .` in `desktop/` fixes formatting.
