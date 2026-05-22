# Browser-Streaming Media Pipeline — Design

> Status: design (brainstorming complete, pre-implementation)
> Date: 2026-05-22
> Supersedes the server-side directory model for the hosted (multi-user) deployment.

## 1. Understanding Summary

- **What:** Replace the server-side directory-scanning model with a **browser-driven, stream-through pipeline**. Each user works from media on **their own computer with no local install**; the ~80 GB VPS only ever holds a few dates' files transiently.
- **Why:** Multiple people share the hosted app, so one person's baked-in directory/spreadsheet defaults are wrong for everyone; and 30 days of 2-minute videos can't all live on the VPS at once.
- **Who:** A small team, shared login (no per-user identity), **one upload run at a time**, each setting their own folders + column mapping per browser session.
- **Constraints:** ≤ ~20 GB transient disk (≈4 dates of media); partial failures continue-and-report; temp files never accumulate; the "Rock Email waits on the YouTube Video URL" rule is preserved within a batch.
- **Non-goals:** Concurrent multi-user uploads; auto-resume of an interrupted run's files; mobile bulk upload; server-side Whisper transcription (removed — see Decision 8).

## 2. Assumptions

1. Media selection uses `<input type="file" webkitdirectory>` per category (desktop browsers; mobile folder-picking is unreliable and out of scope for bulk upload — mobile remains the streamed-login use case).
2. Per-session **settings** (column mapping, selected dates, platform toggles) live in the Flask session; the uploaded **spreadsheet** is cached server-side keyed to the session id and deleted on logout/idle; **media folder selections live in browser memory** (re-pick if the tab closes).
3. Uploaders keep reading from local paths — the orchestrator points them at per-run temp files (minimal uploader changes).
4. Removed/replaced: the `/browse` server endpoint, Settings directory inputs + the per-platform browse buttons, and `config.yaml` `directories` / `sharepoint_docx` / `excel_mapping` defaults.
5. Security: existing auth gate covers all routes; uploaded filenames are sanitized to basename into an isolated per-run temp dir; configurable size caps; temp dir under a dedicated path on the data volume.
6. The 6-digit date-ambiguity handling and all filename date parsing are reused unchanged (now applied to filenames the browser reports).
7. SQLite `upload_history` still records per-row outcomes; the `sessions` resume feature degrades to "re-run remaining dates" because client-side files can't be auto-re-supplied.

## 3. Decision Log

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| 1 | Per-category `webkitdirectory` folder pickers (5: YouTube video, Shorts, Podcast, Thumbnails, Email thumbnails); browser holds file refs, uploads JIT | One parent-folder pick; individual file picks | Matches their actual per-category folder layout; one action per category |
| 2 | Spreadsheet = single-file upload, **persists for the session**; column mapping moves onto the dashboard | Keep mapping in Settings/config | Per-user, self-serve; mapping belongs next to the sheet it describes |
| 3 | Browser sends **filenames only**; server parses dates (reuse `file_scanner`); dashboard shows matched dates per category | Client-side date parsing | Reuse tested logic; names are tiny; nothing heavy moves pre-upload |
| 4 | Upload in **batches of 4 dates** (configurable); platforms run in parallel (max_workers 4) within a batch; dedup by physical file; email waits on YouTube URL within batch | 1 file at a time (serial); 2–3 files in flight | ~20 GB max transient disk while keeping useful parallelism |
| 5 | **Browser-orchestrated** batch loop (upload batch → run → delete → next) | Server-pull over SSE | Airtight disk accounting; resilient to a flaky tab |
| 6 | Partial failures continue-and-report per row; browser→VPS upload failure errors only that file's tasks; temp dirs unique per run, deleted per batch + in `finally` + orphan sweep on startup/idle | Abort whole run on failure | Resilience + the 80 GB guarantee is belt-and-suspenders |
| 7 | **One upload run at a time**; settings per browser session; "busy" guard on overlap | Full per-session concurrency refactor | Small team, occasional batches; avoids a large risky refactor |
| 8 | **Remove Whisper.** Title suggestions use a mapped **transcript column** from the spreadsheet → Ollama; no transcription, no file upload for titles | On-demand Short upload → transcribe → delete | Saves upload time + a whole dependency; transcript text already exists in the sheet |
| 9 | **Chunked uploads** (browser slices each file into ≤~90 MB chunks; server reassembles into the temp file) | Single multipart per file; bypass Cloudflare to the VPS IP | Cloudflare proxies/Tunnel cap request bodies at ~100 MB, so single large-file POSTs would be rejected; we can't bypass (no public VPS port — Tunnel only) |
| 10 | **Idempotent re-run** — skip `(date, platform)` pairs already recorded `success` in `upload_history` for the session; dashboard marks completed dates | Full file-level resume; trust the user not to re-run | Browser-held files can't be auto-resumed; without skip, re-running after a mid-run tab close would duplicate YouTube videos / SimpleCast drafts. (Multi-agent review S1/U1) |
| 11 | **Server-issued opaque `file-id`** (uuid) per physical file at batch-init; chunk endpoint validates `run-id`/`file-id` ownership; per-chunk (~90 MB) and per-run (declared-total) byte caps + free-space precheck | Client filename as id | Prevents path traversal and disk-fill on a tight 80 GB box. (Review C1) |
| 12 | **Run lock auto-releases** on run completion, error (`finally`), and an idle timeout | Manual unlock | A crashed run must not block all future runs. (Review S3) |
| 13 | **Batch "all files reassembled" handshake** — `/media/batch/run` rejected until every `file-id` in the batch reports complete | Fire-and-hope | Avoids the uploader reading a truncated file. (Review S2) |

## 4. Final Design

### 4.1 Components (changed / new / removed)

**New**
- `static/js/dashboard.js` (or inline) — folder/file pickers, filename harvesting, browser-orchestrated batch upload loop, SSE consumption.
- `blueprints/media.py` (or extend `scan.py`) — endpoints:
  - `POST /media/scan` — body: per-category filename lists → returns date→file map (reusing `file_scanner` date parsing).
  - `POST /media/spreadsheet` — multipart xlsx upload → cache server-side per session → return sheet names + columns for mapping.
  - `POST /media/upload/chunk` — chunked file upload (see 4.6): one ≤~90 MB chunk at a time, keyed by `(run-id, file-id, chunk-index, total-chunks)`; the server appends to `temp/<run-id>/<file-id>` and reports completion when the last chunk lands. Used for each batch's files (and would-be large spreadsheets, though those are small).
  - `POST /media/batch/run` — body: which dates/platforms for this batch + the file-id→(category,date) map → runs the existing parallel uploader against the reassembled temp files → SSE progress → deletes the batch temp files on completion.
  - existing `/upload/stream` SSE reused per batch (or one SSE stream spanning batches).
- `core/media_session.py` — per-Flask-session store: mapping, selected dates, cached spreadsheet path, run id; helpers to allocate/cleanup per-run temp dirs.

**Changed**
- `core/session_state.py` — settings (mapping, overrides) sourced from the Flask session, not the global singleton; workflow state stays single-active behind a busy lock.
- `core/upload_jobs.py` — driven per-batch; tasks grouped/deduped by physical temp file; `ReviewEntry` paths point at temp files; email-waits-for-YouTube preserved within the batch's task set.
- `core/excel_parser.py` — reads the uploaded (cached) spreadsheet; mapping includes `transcript_column`.
- `core/llm_title_gen.py` — input is the mapped transcript text (no transcript hashing of audio; cache by text hash unchanged).
- `templates/index.html` (+ review/settings) — folder pickers, spreadsheet pick + column mapping UI, matched-dates display.
- `blueprints/settings.py` + `templates/settings.html` — drop directory inputs, browse buttons, Whisper section; keep schedules, platforms, footers, secrets, password, Connect, LLM status.
- `config.yaml` — remove `directories`, `sharepoint_docx`, `excel_mapping` defaults (keep keys documented as session-set, or remove entirely).

**Removed**
- `core/transcriber.py`, `faster-whisper` from `requirements.txt`, the Whisper Settings section, `/browse` route + directory-picker modal, `directories.base` usage. (ffmpeg in the Docker image can be dropped unless used elsewhere — verify during implementation.)

### 4.2 Data Flow

1. **Setup (dashboard):** user picks 5 category folders (browser holds `File[]`), picks the spreadsheet (uploads once → cached per session → maps columns incl. `transcript_column`).
2. **Scan:** browser POSTs per-category filename lists → server parses dates → returns date→file map → dashboard shows matched dates per category; user selects dates + platforms.
3. **Review:** per-date metadata from the cached spreadsheet; "suggest titles" feeds the mapped transcript text → Ollama (no upload).
4. **Upload (batched, browser-orchestrated):** dates chunked into batches of 4. Per batch: browser uploads the distinct physical files for those dates via `POST /media/upload/chunk` (≤~90 MB chunks, reassembled into `temp/<run-id>/`) → `POST /media/batch/run` → server runs the existing parallel `ThreadPoolExecutor` over the batch's (date,platform) tasks, deduped by file, streaming SSE → on done, server deletes the batch's temp files → browser uploads the next batch.
5. **Finish:** outcomes in `upload_history`; per-run temp dir removed in `finally`; orphan sweep covers abandoned runs.

### 4.3 Error Handling & Cleanup
- Platform failure → per-row error event, batch continues, files still deleted, run continues.
- Browser→VPS file-upload failure → tasks needing that file errored for the batch; rest proceed.
- Temp lifecycle: unique `temp/<run-id>/`; deleted after each batch and in a run-level `finally`; startup + idle sweep removes orphaned `temp/*` dirs. A pre-batch free-space check aborts with a clear message if disk is low.

### 4.4 Security
- All routes behind the existing auth gate.
- **`file-id` is a server-issued uuid** (allocated at batch-init); chunks are written to `temp/<run-id>/<file-id>` — the client never supplies a path, so traversal is impossible. The original filename is carried as metadata only (for extension/date matching), never as a filesystem path.
- Caps: per-chunk ≤ ~90 MB (reject larger); per-run declared-total cap + a **free-space precheck** before accepting a batch (abort with an actionable message if disk is low).
- Scan ignores non-media files (extension allowlist — covers `.DS_Store`/junk that `webkitdirectory` includes).
- Temp dir on the data volume, never web-served.

### 4.5 Testing Strategy
- Unit: filename→date parsing via `/media/scan` (reuse existing date tests); dedup-by-file grouping; batch chunking (4); email-after-YouTube ordering within a batch; temp-dir alloc/cleanup + orphan sweep; transcript-column → Ollama input.
- Route/integration: spreadsheet upload + mapping round-trip in a Flask session; batch upload→run→delete happy path with a fake uploader; partial-failure continue-and-report; busy-guard rejects a second concurrent run.
- Manual (hosted): end-to-end with a couple of throwaway dates, verifying disk stays bounded and temp dirs vanish.

### 4.5b Idempotency, Re-run Safety & Run Lifecycle (from review)
- **Idempotent skip:** before running a `(date, platform)` task, check `upload_history` for a prior `success` in this session/run; if present, **skip** it and emit a `skip` event. Makes re-running a partially-completed batch safe — no duplicate YouTube videos / SimpleCast drafts. The dashboard marks already-completed dates so the user re-selects only what's left.
- **Run lock:** a single in-process run lock; acquired at run start, released in a `finally` on completion/error, and force-released by the idle reaper if a run goes stale. A second concurrent attempt gets a clear "an upload is already running" response.
- **Batch handshake:** the browser uploads all of a batch's files (chunked), each `file-id` reporting `complete`; `/media/batch/run` is rejected (409) until every declared `file-id` is fully reassembled.
- **Keep-tab-open:** the dashboard warns that the tab must stay open for the duration (it holds the files and drives the loop); combined with idempotent skip, an accidental close is recoverable by re-running the remaining dates.

### 4.6 Chunked Upload Protocol (Cloudflare ~100 MB cap)
- Browser slices each file with `File.slice()` into ≤~90 MB chunks (margin under Cloudflare's ~100 MB proxied-body cap) and uploads them **sequentially** per file.
- Each `POST /media/upload/chunk` carries `run-id`, `file-id` (stable per physical file, so dedup still uploads a shared file once), `chunk-index`, `total-chunks`, and the raw chunk bytes. The server appends to `temp/<run-id>/<file-id>.part` and, on the final chunk, renames to the real temp path and verifies the byte count.
- Per-chunk retry on transient failure (bounded); a file that can't complete errors only its dependent tasks (Decision 6).
- Chunk size is configurable; the spreadsheet (small) uses the same path but typically completes in one chunk.
- Reassembly is append-only and confined to the per-run dir; `file-id` is server-namespaced (never a client path).

## 5. Risks
- Large refactor of the core pipeline (scan/upload_jobs/session_state) — mitigated by keeping uploaders' path-based interface and single-active workflow.
- Cloudflare ~100 MB body cap — **resolved by chunked uploads (4.6)**; still verify chunk POSTs flow cleanly through the Tunnel + container Caddy and that SSE and large sequential POSTs coexist.
- Total throughput: every byte traverses browser→VPS→platform, serially across batches; a 30-day run is a long batch job (acceptable, but set expectations).
- Re-run (no resume) is a UX regression for interrupted batches — mitigated by idempotent skip (4.5b); History shows what completed.

## 6. Multi-Agent Design Review (record)

Reviewers invoked sequentially; objections resolved or rejected by the Arbiter.

**Skeptic/Challenger:** S1 duplicate uploads on re-run → **accepted** (idempotent skip, 4.5b / Decision 10). S2 batch race → **accepted** (reassembly handshake, Decision 13). S3 stuck busy-lock → **accepted** (auto-release lock, Decision 12).

**Constraint Guardian:** C1 path-traversal / disk-fill → **accepted** (server-issued `file-id` + byte caps + free-space precheck, Decision 11 / 4.4). C2 orphan sweep must run → **accepted** (wired to startup + idle reaper, 4.3).

**User Advocate:** U1 tab-must-stay-open → **accepted** (explicit warning + idempotent re-run, 4.5b). U2 `webkitdirectory` junk files → **accepted** (extension allowlist, 4.4).

**Rejected / deferred:** full file-level resume (superseded by re-run + idempotency); parallel multi-file chunk upload (YAGNI — sequential is sufficient at current scale).

**Arbiter disposition: APPROVED** with the revisions above folded in. Exit criteria met: Understanding Lock confirmed; all reviewer roles invoked; objections resolved or explicitly rejected; Decision Log complete.
