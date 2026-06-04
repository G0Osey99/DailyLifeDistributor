"""Server-side dispatcher: builds a job_plan envelope for the agent path,
sends it through the relay, and ingests the result stream.

Mirrors core.upload_jobs.run_batch inputs but never calls uploaders
locally — execution happens on the paired agent. See
docs/superpowers/specs/2026-05-22-hybrid-upload-agent-phase3-design.md.
"""
from __future__ import annotations
import logging
from core import db as _db
from core import secrets_store as _ss
from core import image_gatherer as _img

_PROTOCOL_VERSION = 1

# Which secrets_store keys each platform name requires.
# Keys absent from the store are silently omitted from the envelope.
# YouTube credentials are kv secrets (get_secret → str).
# Playwright sessions are blobs (get_blob → bytes) stored under
# "playwright.<basename_no_ext>" by core.playwright_session.
# Real key strings confirmed:
#   youtube.token / youtube.client_secrets  → uploaders/youtube_uploader.py:45-46
#   playwright.rock_session                 → core/playwright_session.py:65-68 + :101
#   playwright.simplecast_session           → core/playwright_session.py:65-68 + :99
#   playwright.vista_social_session         → core/playwright_session.py:65-68 + :100
_PLATFORM_KEYS: dict[str, tuple[str, ...]] = {
    "YouTube Video":  ("youtube.token", "youtube.client_secrets"),
    "YouTube Shorts": ("youtube.token", "youtube.client_secrets"),
    "Rock":           ("playwright.rock_session",),
    "Rock Email":     ("playwright.rock_session",),
    # Canonical platform string is "SimpleCast" (capital C) — the spelling
    # emitted in the run summary (core/session_state.py) and matched by the
    # web dispatch (core/upload_jobs.py). "Simplecast" is kept as a defensive
    # alias so a stray lowercase spelling still resolves the credential.
    "SimpleCast":     ("playwright.simplecast_session",),
    "Simplecast":     ("playwright.simplecast_session",),
    "Vista Social":   ("playwright.vista_social_session",),
}


def _fetch_credential(key: str, *, org_id: int | None) -> str | None:
    """Return the credential string for *key* at the right scope.

    youtube.client_secrets is platform-shared; everything else is per-org.
    kv first; blob fallback.
    """
    if key == "youtube.client_secrets":
        val = _ss.get_platform_secret(key)
        if val is not None:
            return val
        raw = _ss.get_platform_blob(key)
        return None if raw is None else raw.decode("utf-8")
    val = _ss.get_secret(key, org_id=org_id)
    if val is not None:
        return val
    raw = _ss.get_blob(key, org_id=org_id)
    return None if raw is None else raw.decode("utf-8")


def collect_credentials(*, platforms_in_use: set[str],
                        org_id: int | None = None) -> dict[str, str]:
    """Return only the secrets_store entries needed for the given platforms.

    *org_id* defaults to ``effective_org_id()`` so request-context callers
    can omit it. Missing keys are silently omitted.
    """
    if org_id is None:
        from core.org_context import effective_org_id
        org_id = effective_org_id()
    needed: set[str] = set()
    for p in platforms_in_use:
        needed.update(_PLATFORM_KEYS.get(p, ()))
    out: dict[str, str] = {}
    for key in sorted(needed):
        val = _fetch_credential(key, org_id=org_id)
        if val is not None:
            out[key] = val
    return out

# Path fields removed from a serialized ReviewEntry before send; the agent
# re-resolves them from its own scan map.
# Notes on the full list:
#   - youtube_video_path, youtube_shorts_path, podcast_path: primary media paths
#   - thumbnail_path: YouTube/SimpleCast thumbnail
#   - email_thumbnail_path: Rock email thumbnail (separate dir)
#   - spotlight_image_path, vista_image_path, reflection_image_path: planned
#     Rock image fields (not yet on ReviewEntry; harmless to include — stripped
#     only if present in the serialized dict)
_STRIPPED_PATH_FIELDS = frozenset((
    "youtube_video_path",
    "youtube_shorts_path",
    "podcast_path",
    "thumbnail_path",
    "email_thumbnail_path",
    "spotlight_image_path",
    "vista_image_path",
    "reflection_image_path",
))

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Active-job registry — maps job_id → {"queue": Queue}
# ---------------------------------------------------------------------------
import threading as _threading

_jobs: dict[str, dict] = {}
_jobs_lock = _threading.RLock()


def register_job(*, job_id: str, sse_queue, session_id: str | None = None,
                 device_id: str | None = None) -> None:
    """Register an SSE queue for *job_id* so on_frame can route events to it.

    *session_id* is optional; when provided, ``success`` events will be
    written to ``upload_history`` via :func:`core.db.record_upload`.

    *device_id* is optional; recorded so :func:`cancel_job` knows which
    agent to forward the cancel frame to. Web-only-path jobs leave it
    None (cancel for those is a future addition).
    """
    with _jobs_lock:
        _jobs[job_id] = {
            "queue": sse_queue,
            "session_id": session_id,
            "device_id": device_id,
        }


def drop_job(job_id: str) -> None:
    """Remove a job from the registry (call when the SSE stream closes)."""
    with _jobs_lock:
        _jobs.pop(job_id, None)
        _job_dispatch_map.pop(job_id, None)


def _job(job_id: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def on_frame(frame: dict) -> None:
    """Route an incoming relay frame to the appropriate SSE queue.

    Handles ``event``, ``credentials_updated``, and ``image_used`` types.
    Pending results are sent in-band on the agent's hello frame
    (``pending_results``) and ingested in ``blueprints/agent.py``; there
    is no separate ``pending_results_chunk`` wire type.
    Unknown types are logged at debug and dropped.
    """
    ftype = frame.get("type")
    if ftype == "event":
        job = _job(frame.get("job_id", ""))
        if job is None:
            _logger.debug("agent_dispatch.on_frame: event for unknown job %s dropped",
                          frame.get("job_id"))
            return
        if frame.get("event") == "success" and job.get("session_id"):
            try:
                _db.record_upload(
                    job["session_id"],
                    frame.get("iso_date", ""),
                    frame.get("platform", ""),
                    frame.get("payload", {}).get("title", ""),
                    frame.get("payload", {}).get("file_path", ""),
                    True,
                    frame.get("payload", {}).get("watch_url") or frame.get("payload", {}).get("url", ""),
                    frame.get("payload", {}).get("scheduled_time"),
                    "",
                    frame.get("payload", {}).get("external_id"),
                )
            except Exception as exc:
                _logger.warning("record_upload failed: %s", exc)
        # The SSE handler reads ``msg = queue.get()`` then calls
        # ``json.loads(msg)`` — it expects a JSON STRING, not a dict.
        # The web path's emit already does ``json.dumps(payload)``
        # (see blueprints/media.py:emit). Match that contract or the
        # consumer 500s with "JSON object must be str, bytes or
        # bytearray, not dict".
        payload = {k: v for k, v in frame.items() if k not in ("v", "type", "job_id")}
        import json as _json
        try:
            # put_nowait, never a blocking put: the SSE queue is bounded
            # (_QUEUE_MAXSIZE). A closed/stalled dashboard tab stops draining
            # it; a blocking put would then wedge this relay frame-handler
            # thread forever (no further agent frames processed, thread leaks).
            # Mirrors the web path (blueprints/media.py). Dropping a frame
            # under back-pressure is acceptable; hanging the handler is not.
            job["queue"].put_nowait(_json.dumps(payload))
        except Exception as exc:  # noqa: BLE001 — queue.Full or shutdown
            _logger.debug("agent_dispatch: dropped SSE frame (queue full?): %s", exc)
        return
    elif ftype == "credentials_updated":
        key, value = frame.get("key"), frame.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            _logger.warning("credentials_updated: bad shape %r", frame)
            return
        raw_oid = frame.get("org_id")
        cred_org_id: int | None = int(raw_oid) if raw_oid is not None else None
        try:
            if key == "youtube.client_secrets":
                _ss.set_platform_blob(key, value.encode("utf-8"))
            elif key.startswith("playwright."):
                _ss.set_blob(key, value.encode("utf-8"), org_id=cred_org_id)
            else:
                _ss.set_secret(key, value, org_id=cred_org_id)
        except Exception as e:
            _logger.warning("credentials_updated: write failed for %s: %s", key, e)
        return
    elif ftype == "image_used":
        try:
            _db.record_image_use(
                photo_id=frame["photo_id"],
                source=frame["source"],
                topic=frame["topic"],
                used_on_date=frame["used_on_date"],
                photographer=frame.get("photographer", ""),
                photo_url=frame.get("photo_url", ""),
            )
        except Exception as e:
            _logger.warning("record_image_use failed: %s", e)
        try:
            _img.append_credits_entry(
                used_on_date=frame["used_on_date"],
                source=frame["source"],
                photographer=frame.get("photographer", ""),
                photo_url=frame.get("photo_url", ""),
                topic=frame["topic"],
            )
        except Exception as e:
            _logger.warning("append_credits_entry failed: %s", e)
        return
    _logger.debug("agent_dispatch.on_frame: unhandled type %r", ftype)


def _strip_paths(entry_dict: dict) -> dict:
    return {k: v for k, v in entry_dict.items() if k not in _STRIPPED_PATH_FIELDS}


def _gather_rock_image_for_agent(entry, elements: dict, platforms) -> dict | None:
    """Resolve the Rock Vista background image server-side for the agent path.

    Returns a JSON-safe dict (credit metadata + base64 image bytes) the agent
    rehydrates into a ``GatheredImage``, or None when this row doesn't need a
    Rock image or the gather fails (best-effort — the agent then surfaces the
    usual "no usable image" error rather than silently posting without one).
    """
    if "Rock" not in (platforms or []):
        return None
    if not (elements.get("rock_vista", True) and elements.get("rock_image", True)):
        return None
    scripture = (getattr(entry, "scripture", "") or "").strip()
    if not scripture:
        return None
    try:
        from datetime import datetime as _dt
        publish_date = _dt.strptime(entry.date, "%Y-%m-%d").date()
        gi = _img.gather_image_for_verse(
            scripture, publish_date,
            topic_hint=(getattr(entry, "topic_hint", "") or ""),
        )
        if gi is None:
            return None
        import base64 as _b64
        with open(gi.file_path, "rb") as fh:
            image_b64 = _b64.b64encode(fh.read()).decode("ascii")
        # The server's temp download is no longer needed once encoded.
        try:
            import os as _os
            _os.unlink(gi.file_path)
        except OSError:
            pass
        return {
            "photo_id": gi.photo_id,
            "source": gi.source,
            "topic": gi.topic,
            "photographer": gi.photographer,
            "photo_url": gi.photo_url,
            "image_b64": image_b64,
        }
    except Exception:
        _logger.warning(
            "build_envelope: server-side Rock image gather failed for %s",
            getattr(entry, "date", "?"), exc_info=True,
        )
        return None


def build_envelope(
    *,
    job_id: str,
    rows: list[dict],
    entries: dict,        # iso_date -> ReviewEntry
    credentials: dict,    # secrets_store key -> blob string
    config: dict,
    org_id: int | None = None,
) -> dict:
    """Compose the job_plan envelope. Pure function (except org_id default)."""
    if org_id is None:
        from core.org_context import effective_org_id
        org_id = effective_org_id()
    out_rows = []
    for r in rows:
        iso = r["iso_date"]
        entry = entries[iso]
        out_row = {
            "row_idx": r["row_idx"],
            "iso_date": iso,
            "platforms": list(r["platforms"]),
            "elements": r["elements"],
            "entry": _strip_paths(entry.to_dict()),
        }
        # Rock's Vista background image needs the LLM-driven gatherer, which
        # the agent's machine doesn't have. Resolve it here (the server has
        # the LLM + Unsplash/Pexels keys) and ship it in the row so the
        # agent's orchestrator uses it verbatim instead of gathering.
        rock_image = _gather_rock_image_for_agent(
            entry, r.get("elements") or {}, r["platforms"])
        if rock_image is not None:
            out_row["rock_image"] = rock_image
        out_rows.append(out_row)
    return {
        "v": _PROTOCOL_VERSION,
        "type": "job_plan",
        "job_id": job_id,
        "protocol_version": _PROTOCOL_VERSION,
        "config": config,
        "rows": out_rows,
        "credentials": dict(credentials),
        "payload": {
            "org_id": org_id,
            "rows": out_rows,
            "credentials": dict(credentials),
            "config": config,
        },
    }


def _group_summary_by_iso_date(summary: list[dict]) -> list[dict]:
    """Convert per-(date, platform) rows from ``session.get_summary()`` into
    the grouped shape ``filter_done_rows`` expects.

    ``session.get_summary()`` returns one dict per (date, platform) pair:
        {"date": display_date, "iso_date": iso, "platform": "YouTube Video", ...}

    ``filter_done_rows`` (and ``build_envelope`` downstream) expects:
        {"date": iso, "platforms": ["YouTube Video", "Rock"]}

    The web path's ``upload_jobs.run_batch`` happens to iterate per-row
    directly so it never needed this regroup; the agent path does. This
    helper is the only piece of glue between the two shapes.

    Items that are already in the grouped shape (have a ``platforms``
    list) pass through unchanged so the original tests still apply.
    """
    if not summary:
        return []
    # Pass-through path for already-grouped input (test fixtures use this
    # shape directly, and we want filter_done_rows tests to stay valid).
    if any(isinstance(it.get("platforms"), list) for it in summary):
        return summary
    by_iso: dict[str, list[str]] = {}
    for item in summary:
        iso = item.get("iso_date") or item.get("date") or ""
        platform = item.get("platform") or ""
        if not iso or not platform:
            continue
        bucket = by_iso.setdefault(iso, [])
        if platform not in bucket:
            bucket.append(platform)
    return [{"date": iso, "platforms": plats} for iso, plats in by_iso.items()]


def filter_done_rows(*, session_id: str, summary: list[dict]) -> list[dict]:
    """Drop platforms (and entire rows) already recorded as ``success``
    in upload_history.

    Input: session_id, summary in either of two shapes:
      * Grouped: list of ``{"date": iso, "platforms": [...]}``
      * Per-row (from ``session.get_summary()``): list of
        ``{"iso_date": iso, "platform": str, ...}`` — auto-regrouped here.
    Output: list of ``{"row_idx": idx_in_summary, "iso_date": iso,
    "platforms": [remaining]}``, entire row omitted if all platforms done.
    """
    summary = _group_summary_by_iso_date(summary)
    out: list[dict] = []
    for idx, item in enumerate(summary):
        iso = item["date"]
        remaining = [
            p for p in item["platforms"]
            if not _db.has_successful_upload(session_id, iso, p)
        ]
        if remaining:
            out.append({"row_idx": idx, "iso_date": iso, "platforms": remaining})
    return out


# ---------------------------------------------------------------------------
# Device selection + relay dispatch
# ---------------------------------------------------------------------------
import uuid as _uuid
from core import devices as _devices
from core import relay as _relay


class NoAgentOnlineError(RuntimeError):
    """Raised when /upload?path=agent is invoked but no paired agent is online."""


def _relay_online_agents() -> list[dict]:
    """Return online_agents() from the default relay, or [] if none set.

    Wrapped so tests that don't initialise the default relay (most unit
    tests monkeypatch _pick_device directly) don't blow up on the
    AttributeError. The production path always has the default relay
    registered by blueprints.agent at startup.
    """
    try:
        relay = _relay._default_relay
        if relay is None:
            return []
        return relay.online_agents(_relay._default_account)
    except Exception:  # noqa: BLE001 — defensive; same fallback as no agents
        _logger.debug("_relay_online_agents: failed", exc_info=True)
        return []


def _eligible_device_ids() -> set[str] | None:
    """Devices the current request is allowed to dispatch to.

    * Impersonating (``acting_as_org_id`` set): ONLY the program owner's own
      paired devices. The impersonated org's own agents are NOT used for
      these jobs — the explicit support pattern is "owner runs the support
      job on their own machine using the target org's credentials shipped
      in the envelope."
    * Not impersonating: the device pool of the current org — every
      non-revoked device owned by a user with a membership in
      ``effective_org_id``. Prevents org A's job from running on org B's
      agent.
    * Legacy / single-tenant (``LEGACY_PASSWORD_ENABLED`` session with no
      ``user_id`` and no ``current_org_id``): returns ``None`` to signal
      "no filtering" — the USB single-tenant install has exactly one user
      and no tenant model, so the existing system-wide pick is correct.
    """
    from core.org_context import (
        effective_org_id, is_impersonating, real_user_id,
    )
    if is_impersonating():
        uid = real_user_id()
        if uid is None:
            return set()  # impossible-in-practice: impersonation requires a user_id
        return {d["id"] for d in _devices.list_devices_for_user(uid)}
    org_id = effective_org_id()
    if org_id is None:
        return None  # legacy single-tenant — no filter
    return _devices.list_device_ids_in_org(org_id)


def _pick_device(device_id: str | None = None,
                 browser_ip: str | None = None) -> dict:
    """Pick the target device using the explicit-first fallback chain.

    Order (after restricting candidates to the eligible set — see
    :func:`_eligible_device_ids`):

      1. If *device_id* is given AND that device is currently online AND
         eligible → return its row.
      2. If the eligible online set has exactly one device → return it.
      3. If exactly one eligible online device's connect_ip == *browser_ip*
         (same-network) → return it.
      4. Fall back to most_recently_seen_online() (last known good),
         restricted to the eligible set.

    Raises NoAgentOnlineError if no device qualifies (relay empty AND
    no recently-seen eligible row).

    *browser_ip* may be None — that simply disables same-network matching
    so the chain skips straight to step 4.
    """
    eligible = _eligible_device_ids()  # None means "no filter" (legacy)
    online = _relay_online_agents()
    if eligible is not None:
        online = [a for a in online if a["device_id"] in eligible]
    online_ids = {a["device_id"] for a in online}

    # (1) Explicit device_id wins if it's currently online AND eligible.
    if device_id and device_id in online_ids:
        all_rows = {d["id"]: d for d in _devices.list_devices()}
        row = all_rows.get(device_id) or {"id": device_id, "name": "device"}
        _logger.info("_pick_device: explicit device_id=%s", device_id)
        return row

    # (2) Single online (in the eligible set) → trivially pick it.
    if len(online) == 1:
        only_id = online[0]["device_id"]
        all_rows = {d["id"]: d for d in _devices.list_devices()}
        row = all_rows.get(only_id) or {"id": only_id, "name": "device"}
        _logger.info("_pick_device: single-online device_id=%s", only_id)
        return row

    # (3) Same-network — only fires when the browser_ip is known and
    # exactly one eligible online device matches it. Multiple matches →
    # ambiguous, fall through to (4) so we don't silently pick the wrong one.
    if browser_ip and browser_ip != "unknown":
        matches = [a for a in online if a.get("connect_ip") == browser_ip]
        if len(matches) == 1:
            mid = matches[0]["device_id"]
            all_rows = {d["id"]: d for d in _devices.list_devices()}
            row = all_rows.get(mid) or {"id": mid, "name": "device"}
            _logger.info(
                "_pick_device: same-network device_id=%s ip=%s",
                mid, browser_ip,
            )
            return row

    # (4) Most-recently-seen — restricted to the eligible set.
    dev = _devices.most_recently_seen_online()
    if dev is not None and eligible is not None and dev.get("id") not in eligible:
        dev = None  # most-recent-seen exists but doesn't belong to this scope
    if dev is None:
        raise NoAgentOnlineError("no paired agent is online")
    _logger.info("_pick_device: fallback most-recently-seen device_id=%s",
                 dev.get("id"))
    return dev


def start(
    *,
    session_id: str,
    summary: list[dict],
    entries: dict,
    elements: dict,
    config: dict,
    device_id: str | None = None,
    browser_ip: str | None = None,
    job_id: str | None = None,
) -> str:
    """Filter done rows, bundle credentials, build the envelope, and send
    it through the relay to the chosen agent. Returns the job_id used.

    *job_id* (optional) — when supplied, the dispatch reuses that id
    instead of minting a new uuid. The caller in ``blueprints/media``
    pre-registers ``upload_jobs._jobs[job_id]`` BEFORE calling start;
    if start ignores the caller's id and mints its own, the browser
    later asks ``/upload/stream?job_id=<minted>`` for an id that's only
    in ``agent_dispatch._jobs`` (not ``upload_jobs._jobs``), and the
    SSE endpoint 404s. Honoring the caller-supplied id keeps both
    registries in sync.

    *elements* is a dict mapping iso_date -> UploadElements.to_dict().
    Each row receives its own per-date elements slice; rows whose iso_date
    is absent from the map fall back to an empty dict (all defaults apply
    on the agent side).

    *device_id* (optional) — explicit picker selection from the dashboard.
    When provided and currently online, it bypasses the fallback chain.

    *browser_ip* (optional) — the dashboard's _client_ip() at request
    time, used by _pick_device to compute same-network matches.
    """
    from core.org_context import effective_org_id
    org_id = effective_org_id()
    if job_id is None:
        job_id = _uuid.uuid4().hex
    rows = filter_done_rows(session_id=session_id, summary=summary)
    if not rows:
        _logger.info("agent_dispatch.start(job=%s): nothing to do", job_id)
        return job_id
    for r in rows:
        r["elements"] = elements.get(r["iso_date"], {})
    platforms_in_use: set[str] = set()
    for r in rows:
        platforms_in_use.update(r["platforms"])
    creds = collect_credentials(platforms_in_use=platforms_in_use, org_id=org_id)
    envelope = build_envelope(
        job_id=job_id,
        rows=rows,
        entries=entries,
        credentials=creds,
        config=config,
        org_id=org_id,
    )
    device = _pick_device(device_id=device_id, browser_ip=browser_ip)
    # Route by device_id, not device_name: the relay rooms in core/relay.py
    # are keyed by device_id (immutable UUID). device["name"] is the human-
    # readable label ("Mac", "Studio Laptop") which can collide between
    # devices and isn't what register_agent() stores. Passing the name
    # here only worked when name == id (test fixtures) — in production
    # send_to_device would raise ValueError("device not connected").
    # The dashboard chip still shows device_name because the presence
    # broadcast carries it separately.
    _relay.send_to_device(device["id"], envelope)
    # Remember which device received this job so cancel_job() can route
    # the cancel frame to the right room. media.py calls register_job()
    # immediately after start() and inherits this device_id from the
    # dispatch_map; the explicit device_id kwarg on register_job is the
    # canonical path. The dispatch_map is a thin fallback for callers
    # that don't (yet) pass device_id through.
    with _jobs_lock:
        _job_dispatch_map[job_id] = device["id"]
    _logger.info("agent_dispatch.start(job=%s, device_id=%s, name=%s, rows=%d)",
                 job_id, device["id"], device.get("name"), len(rows))
    return job_id


# Snapshot of job_id -> device_id at dispatch time, populated by start()
# and consumed by cancel_job() if register_job didn't carry the device_id.
_job_dispatch_map: dict[str, str] = {}


class JobNotFoundError(Exception):
    """Raised when cancel_job can't locate a target for the given id."""


class AgentOfflineError(Exception):
    """Raised when cancel_job can't reach the target agent (room missing)."""


def cancel_job(job_id: str) -> None:
    """Send a ``cancel_job`` frame to the agent that owns *job_id*.

    Routing precedence:
      1. The device_id stored in the active-jobs registry (set by
         register_job's device_id kwarg) — this is the supported path.
      2. The dispatch-time snapshot recorded by ``start()`` — covers
         the brief window between dispatch and SSE registration.

    Raises:
      JobNotFoundError: no record of this job_id.
      AgentOfflineError: the target agent isn't currently connected to
        the relay. The job may still complete on the agent if it stays
        connected internally; this only reports that the cancel control
        message couldn't be delivered right now.

    Web-only-path jobs aren't supported here yet (their thread lives on
    the server and isn't reachable via the agent relay). The caller is
    responsible for rejecting cancel for those before invoking this.
    """
    device_id: str | None = None
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            device_id = job.get("device_id")
        if device_id is None:
            device_id = _job_dispatch_map.get(job_id)
    if device_id is None:
        raise JobNotFoundError(f"no record of job_id={job_id!r}")
    frame = {"v": 1, "type": "cancel_job", "job_id": job_id}
    try:
        _relay.send_to_device(device_id, frame)
    except ValueError as exc:
        # send_to_device raises ValueError when the device room is missing.
        raise AgentOfflineError(str(exc)) from exc
    _logger.info("cancel_job(job=%s) → device_id=%s", job_id, device_id)


# ---------------------------------------------------------------------------
# C3 — Idempotent pending_results ingest
# ---------------------------------------------------------------------------

def apply_pending_results(entries: list[dict]) -> list[tuple]:
    """Apply each pending-result entry to upload_history idempotently.

    Called when the agent reconnects and sends ``pending_results`` in its
    hello frame.  Each entry has the shape::

        {"job_id": str, "row_idx": int, "iso_date": str,
         "platform": str, "status": "success", "payload": dict}

    The function writes to upload_history only when ``has_successful_upload``
    returns False (idempotent skip).  Every entry that can be matched to a
    known session is always acked so the agent can clear its local buffer.
    Entries whose job_id is not in the registry (job already dropped) are
    also acked — best-effort, server may have already written the row from
    the event stream before the disconnect.

    Returns a list of ``(job_id, row_idx, platform)`` tuples the agent
    should clear from its PendingResults buffer.
    """
    acked: list[tuple] = []
    for e in entries:
        job = _job(e.get("job_id", ""))
        session_id = job.get("session_id") if job else None
        if session_id:
            iso = e.get("iso_date", "")
            platform = e.get("platform", "")
            if not _db.has_successful_upload(session_id, iso, platform):
                try:
                    _db.record_upload(
                        session_id,
                        iso,
                        platform,
                        e.get("payload", {}).get("title", ""),
                        e.get("payload", {}).get("file_path", ""),
                        True,
                        e.get("payload", {}).get("watch_url")
                            or e.get("payload", {}).get("url", ""),
                        e.get("payload", {}).get("scheduled_time"),
                        "",
                        e.get("payload", {}).get("external_id"),
                    )
                except Exception as exc:
                    _logger.warning("apply_pending_results record failed: %s", exc)
                    # Still ack — we tried; re-sending won't help if it keeps failing.
        acked.append((e.get("job_id", ""), e.get("row_idx", 0),
                      e.get("platform", "")))
    return acked
