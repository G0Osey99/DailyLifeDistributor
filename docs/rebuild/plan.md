<!-- Copy of Ryker's private Claude Docs draft "Daily Life Distributor — Rebuild Plan", exported 2026-10-14.
     The doc's three diagrams are written out as text here. If the doc changes, re-export it. -->

# Daily Life Distributor — Rebuild Plan

2026-10-14 · Ryker

Rebuild Daily Life Distributor as one desktop app and retire the hosted web app, the agent relay and the VPS. Use official APIs for YouTube, Wistia, Rock and Vista Social, and keep browser automation only for Spotify. Start with access: a Rock REST key and Vista Social's API add-on decide whether Spotify is the only browser automation left.

## At a glance

*Diagram (text form): one day's pipeline, 3 inputs, 6 destinations, 2 waits.*

```
Spreadsheet row ─┐                       ┌─ YouTube Short (API, scheduled) ──wait──▶ Rock Email / SMS (needs the YouTube link; one draft per date)
Vertical video  ─┼─▶ Day plan ───────────┼─ Spotify (browser automation)
Email thumbnail ─┘   (title approved,    ├─ Vista Social (API, IG + Facebook)              Stock photo (Unsplash / Pexels) ─▶ Rock Daily Experience
                      preflight passed)  └─ Wistia (API, "app YYMMDD") ──wait──────────────────────────────────────────────▶ Rock Daily Experience (needs the Wistia video; parent + 3 child items)
```

Spotify, Vista Social and Wistia need nothing but the plan, so they run in parallel with YouTube.

YouTube and Wistia upload the same vertical file, so the app reads it once; only the two Rock steps have to wait.

## Platform by platform

Five of the six destinations have an official API, so browser automation shrinks from four platforms to one (Spotify), or two if the Vista Social API add-on is too expensive. Each day needs only three inputs: the vertical video, the play-button email thumbnail, and its spreadsheet row.

| Destination | Uses | Recommended integration | Waits for | Change from today | Check before building |
|---|---|---|---|---|---|
| YouTube Short | Vertical video, title, description + #Shorts + footer, tags | YouTube Data API v3: resumable upload, `status.publishAt` scheduling (keep) | — | Horizontal video and custom thumbnail are dropped; Shorts only | Shorts must be vertical and up to 3 minutes; upload quota cost (see Risks) |
| Spotify | Vertical video, episode title, description | Browser automation of the Spotify for Creators web app; there is no public upload API | — | Replaces the SimpleCast automation; no audio-only episodes | Record today's manual upload steps; vertical display and Spotify for Creators hosting are both confirmed |
| Wistia (new) | Vertical video | Wistia Upload API: one POST to upload.wistia.com with a Bearer token, `project_id` = the "Daily Life App" folder, `name` = `app YYMMDD` | — | New destination | A token with upload permission (only the Account Owner can create one) and the folder's hashed ID |
| Vista Social (IG + FB) | Vertical video, description + footer | Vista Social API: `POST /v2/posts/save` with an `x-api-key`, one post to both profiles with a schedule time | — | Today it is browser automation that already posts the vertical video | The API is a paid add-on; confirm Reels/video publishing and the schedule time format |
| Rock Daily Experience | Spreadsheet, stock image, Wistia video | Rock REST API with a REST key: create the parent item and its Reflection, Vista and Spotlight children, then set attribute values | Wistia, then Rock's Wistia sync | Moves from browser automation to REST. Spotlight already picks the Wistia video by name (folder "Daily Life App", element "app YYMMDD"); the upload to Wistia becomes the app's job | Whether a REST key will be issued; how Spotlight finds the video |
| Rock Email / SMS | Email thumbnail, description, YouTube link | Rock REST API, same client: one draft item per date | YouTube | Same rules; the link is now the Short's URL | The footer: the Add form pre-fills it today, so over REST we read the attribute's default value instead |

Two things follow from the table. First, YouTube and Wistia upload the same file, so the app reads each video once and fans it out. Second, only Spotify (and Vista Social, without the add-on) needs a logged-in browser, which is the main reason the app should run where the files and logins already are: on the user's computer.

## Desktop vs. web

Drop the hosted web app. Most of its complexity exists to move media and logins from the user's computer to a server and back, and the new flows need neither.

| What the web app carries today | Why it exists | In a desktop app |
|---|---|---|
| Chunked browser-to-VPS streaming, run lock, temp dirs, orphan sweep, free-space checks | Media lives on the user's machine; the VPS has ~80 GB | Files are read in place. All of it goes away |
| VNC / Xvfb remote-login flow | Playwright logins had to happen on a headless server | The login window opens on the user's screen |
| Relay, pairing codes, HWID, same-network picker, whoami, cancel frames | Bridging the web UI to the local agent | Gone: the UI and the engine are one app |
| Orgs, roles, invitations, 2FA, recovery codes, audit log, outbound email | Multi-tenant SaaS on a public URL | The OS account and keychain protect it |
| Docker, Caddy, Cloudflare tunnel, Ollama container, deploy runbook | Hosting | GitHub Releases for downloads and auto-update |

Three things the web app gave us need another answer:

- **Using it from any computer.** One person runs it on one Mac, so it installs there and nowhere else. Scheduling happens on the platforms (`publishAt` and their equivalents), so the app only needs to be awake while it uploads, not at publish time.
- **Several people helping.** Not needed today, since one person runs it. If that changes, the app can write approved titles and per-platform status back to the OneDrive spreadsheet, where the team already looks.
- **Running unattended.** A "station" mode on an always-on studio Mac watches the media folder and ships each day once its title is approved.

Selling to other churches is a maybe-later; if it happens, a desktop app with a license key and auto-updates is far less to run than a multi-tenant SaaS. Nothing else needs to stay in the cloud.

## Architecture

*Diagram (text form): one app on the user's computer; only Spotify still needs a browser.*

```
On this computer                Desktop app (Electron) — ships as one signed, auto-updating app
  Media folder (videos,   ──▶     UI: week board, approvals, preview, connections, activity log
    thumbnails)                   Engine (background process)
  Spreadsheet (OneDrive   ──▶       Planner (matches files to spreadsheet rows)    Connectors (one per platform: post, verify, edit) ──▶ YouTube API, Wistia API,
    .xlsx)                          Job runner (dependency order, retries,          Browser runner (Playwright + Chrome,                  Rock REST API, Vista Social API,
                                      breakers)                                       one profile each) ─────────────────────────────────▶ Claude API, Unsplash/Pexels
                                  SQLite (series, days, tasks, events, history)     OS keychain (tokens and API keys)                      Spotify web app (browser)
```

The data model keeps it general. A **series** holds its sources, templates and destinations; a **day** holds its row, files and approved title; a **task** is one day on one destination, with its state (waiting, ready, running, scheduled, verified, failed, needs login) and the IDs or links it produced.

Each connector declares what it needs and what it produces: YouTube produces a watch link, Rock Email needs one. The runner derives the order from those declarations instead of hard-coding it, so a new destination or a second series is configuration, not code.

## Tech stack

Pick Electron with TypeScript end to end. Of today's ~34k lines of app code (plus ~21k lines of tests), roughly 4–5k are domain logic worth carrying over; the rest is web, relay and tenant plumbing the rebuild drops, so reusing Python buys less than it seems.

| Option | Browser automation | UI | Packaging and updates | Reuse of today's code | Download |
|---|---|---|---|---|---|
| **Electron + TypeScript (pick)** | Playwright's primary SDK is Node; logins can open in an app window | Web UI (React or Svelte) | electron-builder + electron-updater from GitHub Releases; mature signing and notarization | Port the domain logic and its test cases | Large: bundles Chromium |
| Tauri 2 + TypeScript UI | Needs a Node or Python sidecar for Playwright: two runtimes to ship | Web UI in the OS webview | Built-in updater; sidecar signing adds work | Same as Electron | Small shell, plus the sidecar |
| Python engine + webview or Qt UI (evolve today's agent) | Playwright for Python, as today | pywebview or PySide6; today's agent uses customtkinter | PyInstaller universal2 plus the custom signed-manifest updater; about 11 of the last 50 commits were release and packaging fixes | Highest: uploaders move over as-is | Medium |

The pieces under the pick:

- **Engine** in an Electron utility process, so long uploads never freeze the UI; the OS is kept awake while a run is active.
- **Browser automation** with playwright-core driving the installed Chrome, one persistent profile per platform in the app's data folder.
- **APIs**: googleapis for YouTube; plain HTTPS clients for Wistia, Rock and Vista Social.
- **Storage**: SQLite (better-sqlite3) with versioned migrations; tokens and keys in the OS keychain through Electron's safeStorage, replacing the Fernet master key.
- **Media checks**: a bundled ffprobe to read each video's aspect ratio, duration and codecs before anything uploads.
- **Titles**: a current Claude model over the API by default, with an optional local OpenAI-compatible endpoint for offline use. The bundled llamafile and the USB Python builds go away.
- **Tests**: Vitest for the engine, Playwright for the UI; today's filename-date and Rock text test cases become fixtures.

If staying in Python matters more than the shell, the third option still supports everything else in this plan. Being Mac-only also means one universal build to sign and notarize, with no Windows release matrix.

## Workflow and UX

The app becomes a week board you keep open, not a form you refill each session: it remembers the folders, spreadsheet and mapping, and shows every day's readiness at a glance.

A week in the new app:

1. **Set up once.** A series ("Daily Life") stores its media folder, the OneDrive spreadsheet, column mapping, text templates and default publish times. Today these are re-picked per browser session.
2. **Watch the week fill in.** The board lists the next 14 days: video, email thumbnail, spreadsheet row, title, then one status chip per platform. The app watches the folder and spreadsheet and flags gaps ("Thu Oct 23 has no video and publishes in 2 days").
3. **Approve titles.** Five suggestions per day from the transcript; staff pick or edit one and mark it approved. Today the first suggestion auto-fills and starting the upload counts as approval.
4. **Preview.** One card per platform shows exactly what will post: YouTube title and description, the Instagram/Facebook caption, the Spotify episode, the email draft, and the three Rock items with their stock photo (swap it here).
5. **Preflight.** One check before anything uploads: video is 9:16 and at most 3 minutes, captions fit each platform, required fields exist, times are in the future, every login and token is alive, quota is enough. Each failure comes with a fix.
6. **Ship.** One button runs the whole selection, ordered by its dependencies, with a progress lane per platform, retries with backoff, and a per-platform circuit breaker.
7. **Verify.** The app reads each day back from every platform (title, scheduled time, visibility) and marks it Verified, or flags the mismatch. This absorbs today's separate calendar refresh.
8. **Hear about it.** An OS notification, plus an optional email or Slack summary with every link.

New features worth building:

- **Edit once, update everywhere.** Change a title, caption or time after shipping and push it to each platform that allows updates (YouTube, Wistia, Rock, Vista Social), with Spotify through automation.
- **Pull a day.** Unschedule one date across every platform, behind a confirmation.
- **Login health.** A Connections page checks each login on a daily timer and before every run, so a 2FA prompt never interrupts a batch.
- **Assisted mode.** When automation fails on Spotify or Vista Social, the app opens the form pre-filled and a person clicks Publish; the day still ships.
- **Failure evidence.** A screenshot and page snapshot attach to any failed browser task.
- **Status write-back.** Approved titles and per-platform links go back into the OneDrive spreadsheet through Microsoft Graph, if more people join later.
- **Station mode.** On an always-on Mac, ship each approved day automatically as its files arrive.
- **Templates with live preview** for titles, descriptions and footers per platform, and **export/import** of a whole series setup for another series or another church.

## Keep, drop, add

Most of today's code goes away, and nearly all of it is hosting, bridging and tenant plumbing rather than publishing logic. Of the last 50 commits, roughly 20 fixed the agent relay or its packaging and about 15 fixed the Vista Social and Rock uploaders: the two areas this plan removes or shrinks.

| Today's feature | Verdict | Note |
|---|---|---|
| Filename date matching (five formats; ambiguous 6-digit dates listed under both) | Keep | Port it with its test cases as fixtures |
| Per-platform circuit breaker | Keep | One per connector, same thresholds |
| Stock photo lookup (Unsplash/Pexels, LLM keywords) and credits ledger | Keep | Add a swap button in the preview |
| YouTube Shorts upload (resumable, `publishAt`, quota tracking) | Keep | Today's code is the reference implementation |
| Rock text rules: item titles, "Pause & Pray" labels, email message above the footer | Keep | Move into editable templates |
| Spreadsheet column mapping (14 keys) | Rework | Saved per series; read the OneDrive-synced copy on disk; Spotify takes the episode-title column; retire the YouTube-title and podcast-title columns if nothing uses them |
| Title suggestions (local llamafile, 5 per day) | Rework | An explicit Approved state; a Claude model by default |
| Preflight (session file exists, LLM, image keys, row checks, quota) | Rework | Add ffprobe media checks and a real login check per platform |
| Email waits for YouTube | Rework | Becomes a general dependency graph that also covers Wistia before Rock |
| Idempotent skip from local history | Rework | Confirm against the platform, so a deleted post re-ships and a manual post is not duplicated |
| Calendar refresh | Rework | Becomes the Verify step after every run |
| Rock Daily Experience and Email (browser automation, hard-coded control ids) | Rework | REST API; keep the browser path as a fallback |
| Vista Social (browser automation) | Rework | API if the add-on is approved |
| Encrypted secret store (Fernet master key) | Rework | OS keychain |
| Agent auto-update (signed manifest) | Rework | electron-updater on GitHub Releases |
| Wistia upload | Add | Upload API, one request per day |
| Spotify for Creators | Add | Browser automation with an assisted-mode fallback |
| SimpleCast | Drop | Replaced by Spotify |
| YouTube horizontal video and custom thumbnails | Drop | Not in the new flows |
| Hosted web app: Flask, 33 templates, landing page, chunked streaming pipeline | Drop | Files are read in place |
| Relay, pairing codes, HWID, device picker, cancel frames | Drop | One app, nothing to bridge |
| Multi-tenant: orgs, roles, invitations, 2FA, recovery, audit log, outbound email | Drop | The OS account and keychain cover it |
| VNC remote login and the VPS (Docker, Caddy, Cloudflare tunnel, Ollama) | Drop | Logins happen on the user's own screen |
| Bundled llamafile and USB Python builds | Drop | A hosted model, or the user's own local one |

## Roadmap

Build the engine before the app: a command-line engine that ships one real day as drafts proves every integration before any UI exists.

*Diagram (text form): the old app stays live until two clean weeks on the new one. Four phases, each closed by a gate.*

| Phase | Work | Gate to leave it |
|---|---|---|
| 0 · Unblock | record Spotify steps; Rock REST key; Vista API price; Wistia token and folder; stack decision | every key and test result in hand |
| 1 · Engine | command-line engine; planner, job runner; YouTube, Wistia; Rock REST, Vista API | one real day shipped as drafts |
| 2 · App | week board, approvals; preview, preflight; Spotify automation; in-app logins | a full week as drafts |
| 3 · Cutover | signed builds; auto-update, verify; run beside old app; retire the VPS | two clean weeks, then the VPS goes off |

My rough guess for one developer working with Claude Code is about two months to cutover; Phase 0 is mostly waiting on other people, so start it now. After cutover: edit-everywhere, the template editor, spreadsheet write-back, a second series, and packaging for other churches.

## Risks and open questions

The biggest open risk is access: a Rock REST key and the Vista Social API add-on decide how much browser automation is left. Spotify is settled: vertical episodes already display correctly there.

| Risk | What breaks | Mitigation |
|---|---|---|
| No Rock REST key | The REST plan for both Rock flows | Keep today's browser automation for Rock, which works but depends on hard-coded control ids |
| Rock hasn't synced the new Wistia video yet | Spotlight can't select "app YYMMDD" | Upload to Wistia days ahead of Rock, or trigger the sync; find out how often Rock's Wistia sync job runs |
| Vista Social API is a paid add-on | The API plan for Vista Social | Get the price; otherwise keep the browser automation, with assisted mode |
| YouTube upload quota | Uploading a week at once | Sources say an upload dropped from ~1,600 to ~100 units on Dec 4, 2025 (today's code already assumes 100). If not, 7 uploads cost 11,200 units, over the 10,000 daily default; spread them across days |
| Shorts classification is YouTube's call | Publishing as a regular video | Preflight enforces 9:16 and at most 3 minutes |
| Desktop distribution | Install and updates | An Apple Developer ID for signing and notarization (Mac only, so no Windows certificate); keep the Mac awake during runs |
| Rewriting a working tool | A missed week | Run old and new side by side, the new one posting drafts, until two clean weeks |

Answers to the open questions, Oct 14, 2026:

| Question | Answer | What it changes |
|---|---|---|
| Which machines? | Mac only | One universal build to sign and notarize; the Windows release matrix goes away |
| Who runs it? | One person on one machine | All state lives on that Mac; no sync or sharing layer |
| Where is the spreadsheet? | OneDrive / SharePoint | Read the OneDrive-synced copy on disk, with no Microsoft login; use Microsoft Graph only if status write-back is wanted later |
| Podcast after SimpleCast? | Phased out. The show is hosted on Spotify for Creators, and the vertical video, with its audio, goes there directly | No RSS feed or Apple Podcasts listeners to protect; the app creates each episode directly in Spotify for Creators |
| Other churches? | Maybe later | Keep the series model and templates general where it is cheap; build for LCBC first, with no licensing work now |
| Spotify title? | The episode-title column | Spotify gets the same title as Rock's Spotlight; the approved Shorts title stays on YouTube |
| Does vertical video display well on Spotify? | Yes, checked on a live episode | No 16:9 copy needed; the same vertical file goes to every platform |

## Sources

The network policy where this was written blocked opening these pages directly; platform facts come from search summaries of them, so confirm each before building on it. Code facts come from the DailyLifeDistributor repository and the new flows from the Daily Life Platform Flows PDF.

- Spotify for Creators: [Publishing videos](https://support.spotify.com/us/creators/article/publishing-videos/), [Video specs](https://support.spotify.com/us/creators/article/video-specs/), [Publishing video episodes if you're not hosted with Spotify](https://support.spotify.com/us/creators/article/video-episodes-for-shows-not-hosted-with-spotify/)
- Lower Street: [Spotify Video Podcast Guide](https://lowerstreet.co/blog/spotify-video-podcasts)
- Wistia: [Upload API](https://docs.wistia.com/docs/wistia-upload-api)
- Vista Social: [Vista Social API (support article)](https://support.vistasocial.com/hc/en-us/articles/32993061787035-Vista-Social-API), [API reference](https://apidocs.vistasocial.com/)
- Rock RMS: [The Rock REST API](https://community.rockrms.com/developer/303---blast-off/the-rock-rest-api), [Q&A on content channel item attributes](https://www.rockrms.com/ask/developing/1217)
- Triumph Tech: [Wistia Media Sync for Rock](https://triumph.tech/documentation/wistia-media-sync)
- YouTube: [Quota costs](https://developers.google.com/youtube/v3/determine_quota_cost), [ChannelCrawler on the Dec 2025 upload-cost change](https://channelcrawler.com/insights/youtube-api-daily-limit-quotas-costs-and-how-to-scale-beyond-10000-units-channelcrawler), [Async on Shorts length](https://async.com/blog/how-long-can-youtube-shorts-be/)
