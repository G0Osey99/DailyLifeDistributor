"""Pre-upload readiness check.

Before committing a multi-date upload run, the operator hits
``GET /preflight/check`` and gets a per-platform red/green verdict with
specific remediation. The point is to catch a rotted session, a missing
API key, or an unreachable LLM in 30 seconds — at 8am Monday — instead of
discovering it three dates into a 28-date batch.

Design choices:
  * Cheap by default. Session checks report whether a saved Playwright
    session blob exists; they do NOT launch Chrome to prove it's still
    valid (that's slow and flaky). The detail string says so honestly.
  * Per-org scoped. Credentials live in the per-org secrets store, so the
    checks resolve against ``effective_org_id()``.
  * Non-destructive. No uploads, no scheduling, no email. The only network
    call is an optional LLM ``/v1/models`` probe (already part of the
    is_llamafile_running health check) and, when ``?probe=1`` is passed,
    one tiny Unsplash search.
  * Honest blocking flags. ``blocking: true`` means "this platform cannot
    run at all without fixing it." Image-provider keys are non-blocking for
    everything except Rock's Vista child.

The route is auth-gated by the app-level ``_require_auth`` before_request
hook (same as every other dashboard route).
"""
from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

bp = Blueprint("preflight", __name__)


# secrets_store keys per platform (Playwright session blobs live under
# "playwright.<name>"; YouTube creds are a kv token).
_SESSION_KEYS = {
    "simplecast": "playwright.simplecast_session",
    "vista_social": "playwright.vista_social_session",
    "rock": "playwright.rock_session",
    "rock_email": "playwright.rock_session",  # shares the Rock session
}

# Friendly platform labels for the response.
_LABELS = {
    "youtube_video": "YouTube Video",
    "youtube_shorts": "YouTube Shorts",
    "simplecast": "SimpleCast",
    "rock": "Rock",
    "rock_email": "Rock Email",
    "vista_social": "Vista Social",
    "llm": "Title/Image LLM",
    "image_provider": "Stock images (Rock Vista)",
}

# The platform keys a caller can ask about, in display order.
_ALL_PLATFORMS = [
    "youtube_video", "youtube_shorts", "simplecast",
    "rock", "rock_email", "vista_social",
]


def _resolve_image_key(org_id):
    """Mirror image_gatherer._resolve_key: per-org secret first, then env."""
    from core import secrets_store
    for name in ("UNSPLASH_ACCESS_KEY", "PEXELS_API_KEY"):
        val = (secrets_store.get_secret(name, org_id=org_id)
               or os.environ.get(name, "") or "").strip()
        if val:
            return name, val
    return None, ""


def _check_youtube() -> dict:
    """YouTube token present + valid-or-refreshable (no OAuth, no API call)."""
    try:
        from uploaders import youtube_uploader
        if youtube_uploader.is_authenticated():
            return {"ok": True, "status": "Authenticated",
                    "detail": ("Token present and refreshable — not API-validated "
                               "here. A revoked grant still reads green; if uploads "
                               "401, re-connect YouTube in Settings."),
                    "blocking": True}
        return {
            "ok": False, "status": "Not authenticated",
            "detail": ("No usable YouTube token. Open Settings → Connect "
                       "YouTube and complete sign-in, then re-check."),
            "blocking": True,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": "Check failed",
                "detail": f"Could not evaluate YouTube auth: {e}",
                "blocking": True}


def _check_session(platform: str, org_id) -> dict:
    """Playwright session-blob presence (validity not verified without Chrome)."""
    from core import secrets_store
    key = _SESSION_KEYS[platform]
    try:
        present = secrets_store.has_secret(key, org_id=org_id) or bool(
            secrets_store.get_blob(key, org_id=org_id)
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": "Check failed",
                "detail": f"Could not read the saved session: {e}",
                "blocking": True}
    label = _LABELS.get(platform, platform)
    # Rock Email shares the Rock login session, so its remediation points at
    # the "Connect Rock" control (there is no separate Rock Email connect).
    connect_label = "Rock" if platform == "rock_email" else label
    if present:
        return {
            "ok": True, "status": "Session present",
            "detail": ("A saved login session exists. Validity isn't verified "
                       "here — if the upload reports 'session expired', "
                       f"re-connect {connect_label} from Settings."),
            "blocking": True,
        }
    return {
        "ok": False, "status": "No saved session",
        "detail": (f"No saved {connect_label} login. Open Settings → Connect "
                   f"{connect_label} and sign in once, then re-check."),
        "blocking": True,
    }


def _check_llm() -> dict:
    """LLM backend reachable + the configured model exists in /v1/models."""
    try:
        import requests
        from core.llm_title_gen import (
            LLM_BASE_URL, LLM_MODEL, is_llamafile_running,
        )
        if not is_llamafile_running():
            return {
                "ok": False, "status": "Unreachable",
                "detail": (f"The LLM at {LLM_BASE_URL} did not respond. Title "
                           "auto-suggest and Rock image keywords will be "
                           "skipped. Start llamafile / Ollama or set "
                           "LLM_BASE_URL."),
                "blocking": False,
            }
        # Confirm the configured model is actually served — the Ollama
        # 'model not found' bug that broke Rock images was invisible until
        # an upload ran.
        model_ok = True
        model_detail = ""
        try:
            r = requests.get(f"{LLM_BASE_URL}/v1/models", timeout=5)
            if r.status_code < 500:
                ids = [m.get("id") for m in (r.json().get("data") or [])]
                # LLM_MODEL "local" is the llamafile wildcard — always fine.
                # Ollama lists models with their tag ("llama3.2:latest"), so a
                # config of LLM_MODEL="llama3.2" must match "llama3.2:latest"
                # (and vice-versa). Exact-match-only was a false-RED that scared
                # operators off a working setup.
                def _model_served(want, served):
                    for got in served:
                        if not got:
                            continue
                        if got == want:
                            return True
                        # tag-prefix either direction: want "llama3.2" ~ "llama3.2:latest"
                        if got.split(":", 1)[0] == want.split(":", 1)[0]:
                            return True
                    return False
                if LLM_MODEL != "local" and ids and not _model_served(LLM_MODEL, ids):
                    model_ok = False
                    model_detail = (
                        f"Configured model {LLM_MODEL!r} is not in the served "
                        f"list {ids}. Rock image keywords will fail. Pull the "
                        f"model or fix LLM_MODEL.")
        except Exception:  # noqa: BLE001 — model list is best-effort
            pass
        if not model_ok:
            return {"ok": False, "status": "Model missing",
                    "detail": model_detail, "blocking": False}
        return {"ok": True, "status": "Reachable",
                "detail": f"{LLM_BASE_URL} (model {LLM_MODEL})", "blocking": False}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": "Check failed",
                "detail": f"Could not evaluate the LLM backend: {e}",
                "blocking": False}


def _check_image_provider(org_id, probe: bool = False) -> dict:
    """Stock-image key present (and optionally one live Unsplash search)."""
    name, key = _resolve_image_key(org_id)
    if not key:
        return {
            "ok": False, "status": "No key",
            "detail": ("No UNSPLASH_ACCESS_KEY or PEXELS_API_KEY. Rock's Vista "
                       "child needs a background image — set a key under "
                       "Settings (or env) or the Rock upload will fail on the "
                       "image step."),
            "blocking": False,
        }
    if not probe or name != "UNSPLASH_ACCESS_KEY":
        return {"ok": True, "status": f"{name} set",
                "detail": "Key present (not live-tested).", "blocking": False}
    # Optional live probe — one tiny search to confirm the key works.
    try:
        import requests
        r = requests.get(
            "https://api.unsplash.com/search/photos",
            headers={"Authorization": f"Client-ID {key}"},
            params={"query": "calm", "per_page": 1},
            timeout=8,
        )
        if r.status_code == 200:
            return {"ok": True, "status": "Unsplash OK",
                    "detail": "Live search succeeded.", "blocking": False}
        return {"ok": False, "status": f"Unsplash {r.status_code}",
                "detail": (f"Unsplash returned HTTP {r.status_code}. The key "
                           "may be invalid or rate-limited."),
                "blocking": False}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": "Probe failed",
                "detail": f"Live Unsplash probe failed: {e}", "blocking": False}


def run_preflight(platforms=None, org_id=None, probe: bool = False) -> dict:
    """Evaluate readiness for *platforms* (default: all). Pure-ish: no uploads.

    Returns ``{"ok": bool, "checks": {key: {...}}}``. ``ok`` is True only when
    every *blocking* check for a requested platform passes; non-blocking
    checks (LLM, image provider) lower ``ok`` is NOT gated on them, but they
    still appear so the operator sees the warning.
    """
    if org_id is None:
        try:
            from core.org_context import effective_org_id
            org_id = effective_org_id()
        except Exception:  # noqa: BLE001
            org_id = None

    requested = [p for p in (platforms or _ALL_PLATFORMS) if p in _ALL_PLATFORMS]
    if not requested:
        requested = list(_ALL_PLATFORMS)

    checks: dict = {}

    # YouTube video/shorts share one token.
    yt_needed = any(p in ("youtube_video", "youtube_shorts") for p in requested)
    if yt_needed:
        checks["youtube"] = _check_youtube()

    for p in ("simplecast", "vista_social", "rock", "rock_email"):
        if p in requested:
            checks[p] = _check_session(p, org_id)

    # Rock pulls a stock image for its Vista child; surface the key + LLM
    # readiness whenever Rock (in-app) is in the run.
    if "rock" in requested:
        checks["image_provider"] = _check_image_provider(org_id, probe=probe)

    # LLM is used for title suggestions (any YouTube/SimpleCast title) and
    # Rock image keywords — always worth surfacing.
    checks["llm"] = _check_llm()

    # ok is gated only on BLOCKING checks.
    ok = all(c.get("ok") for c in checks.values() if c.get("blocking"))
    return {"ok": ok, "checks": checks}


# ---------------------------------------------------------------------------
# Per-row validation dry-run
# ---------------------------------------------------------------------------
# Unlike the platform readiness check above (sessions/keys/LLM), this walks
# every (date, platform) the run WOULD dispatch and confirms the data is
# actually there — the right media files matched, the required Excel columns
# populated, the Wistia ref inferable — WITHOUT launching a single uploader.
# It catches the failure class that bit the user (missing wistia_ref, blank
# fields, absent files) at the speed of a scan, before committing a 28-date
# batch.

# Which scan category each platform's primary media comes from.
_PLATFORM_MEDIA_CATEGORY = {
    "youtube_video": "youtube_video",
    "youtube_shorts": "youtube_shorts",
    "simplecast": "podcast",
    "vista_social": "youtube_shorts",  # Vista reposts the Shorts clip
}


def _first_name(cat_map: dict, category: str) -> str | None:
    names = (cat_map or {}).get(category) or []
    return names[0] if names else None


def _validate_row(platform: str, cats: dict, meta: dict,
                  platforms_for_date: set) -> list[str]:
    """Return a list of blocking data issues for one (date, platform).

    Empty list == this row has everything it needs to attempt the upload.
    Mirrors the precondition checks the real uploaders / session_state gate
    on, but reads from the scan + cached spreadsheet so it never uploads.
    """
    issues: list[str] = []

    # Media-file presence for the file-backed platforms. Folder labels match
    # exactly what the dashboard's folder pickers are named, so the message
    # points the user at the right control.
    cat = _PLATFORM_MEDIA_CATEGORY.get(platform)
    if cat and not _first_name(cats, cat):
        folder = {"youtube_video": "Horizontal Video",
                  "youtube_shorts": "Vertical Video (Shorts)",
                  "podcast": "Podcast Audio"}.get(cat, cat)
        issues.append(f"no file matched in the '{folder}' folder for this date")

    if platform == "simplecast":
        # The episode title resolves from podcast_title || youtube_title. With
        # both blank the uploader would push a blank-titled episode — a real
        # failure the old file-only check missed (false-GREEN).
        if not ((meta.get("podcast_title") or "").strip()
                or (meta.get("youtube_title") or "").strip()):
            issues.append("no episode title — fill the podcast-title or "
                          "youtube-title column for this date")

    if platform == "vista_social":
        # Vista's caption falls back to the description, then a title. Only
        # flag when there is NO text anywhere (an all-blank post is almost
        # always a mistake); a filled description alone is fine.
        if not any((meta.get(k) or "").strip() for k in
                   ("vista_caption", "description", "shorts_title", "youtube_title")):
            issues.append("no caption/description text — the Vista post would "
                          "have no words (fill vista-caption or description)")

    if platform == "rock":
        # Wistia ref is inferred from the Shorts filename — the exact failure
        # the user hit. Reproduce the inference here so we catch it now.
        from core.session_state import infer_wistia_ref
        shorts = _first_name(cats, "youtube_shorts")
        if not shorts:
            issues.append("no Shorts file → can't infer the Wistia ref "
                          "(Spotlight needs 'app YYMMDD')")
        elif not infer_wistia_ref(shorts):
            issues.append(f"Shorts filename {shorts!r} has no 6-digit date code, "
                          "so the Wistia ref can't be inferred (expected like "
                          "'app 260601.mp4')")
        if not ((meta.get("episode_title") or "").strip()
                or (meta.get("youtube_title") or "").strip()):
            issues.append("no episode_title (Spotlight title) — map the Excel "
                          "title/episode column")
        if not (meta.get("passage") or "").strip():
            issues.append("no passage (Vista verse ref) — map the Excel passage column")
        if not (meta.get("scripture") or "").strip():
            issues.append("no scripture (Vista verse text) — map the Excel scripture column")
        if not (meta.get("prayer") or "").strip():
            issues.append("no prayer (Reflection) — map the Excel prayer column")

    if platform == "rock_email":
        # The email needs the horizontal YouTube watch URL. In a run it comes
        # from the YouTube Video upload for the same date; absent that, there's
        # no source (the metadata has no watch-url column by default).
        has_yt_video = (
            "youtube_video" in platforms_for_date
            and bool(_first_name(cats, "youtube_video"))
        )
        if not has_yt_video:
            issues.append("no YouTube Video in this run for the date → no watch "
                          "URL for the email (enable YouTube Video, or it errors)")

    return issues


def validate_run(dates, platforms, scan, org_id=None) -> dict:
    """Validate every (date, platform) the run would dispatch — data only.

    *scan* is the browser's scan result filtered to the selected dates:
    ``{iso: {"categories": {cat: [names]}, "metadata": {...}}}``.

    Returns ``{"ok": bool, "rows": [{date, platform, ok, issues}], "note": str}``.
    ``ok`` is True only when every row is issue-free. Sessions/keys are NOT
    re-checked here (use /preflight/check) — this is purely about whether the
    data for each date is complete.
    """
    requested = [p for p in (platforms or []) if p in _ALL_PLATFORMS]
    rows: list[dict] = []
    yt_uploads = 0  # count of videos.insert this run would issue
    for iso in (dates or []):
        slot = (scan or {}).get(iso) or {}
        cats = slot.get("categories") or {}
        meta = slot.get("metadata") or {}
        plats_for_date = set(requested)
        for platform in requested:
            issues = _validate_row(platform, cats, meta, plats_for_date)
            rows.append({
                "date": iso,
                "platform": _LABELS.get(platform, platform),
                "ok": not issues,
                "issues": issues,
            })
            # Count an actual upload only when the media file is present (a
            # fileless YT row won't reach videos.insert).
            if platform == "youtube_video" and _first_name(cats, "youtube_video"):
                yt_uploads += 1
            elif platform == "youtube_shorts" and _first_name(cats, "youtube_shorts"):
                yt_uploads += 1
    ok = all(r["ok"] for r in rows) if rows else False
    return {
        "ok": ok,
        "rows": rows,
        "quota": _estimate_youtube_quota(yt_uploads, org_id),
        "note": ("Data-only check — login sessions and API keys are validated "
                 "separately by the readiness check."),
    }


def _estimate_youtube_quota(yt_uploads: int, org_id=None) -> dict:
    """Estimate the YouTube Data API quota this run would consume vs today's
    remaining cap. The single biggest Monday gotcha: videos.insert costs 1600
    units and the default daily quota is 10,000, so only ~3 dates' worth of
    (Video + Shorts) fit in a day. Surfacing this BEFORE the run prevents the
    "first 3 dates upload, the rest fail quotaExceeded" surprise.
    """
    try:
        from core.quota import QUOTA_COSTS, DAILY_QUOTA, get_quota_used
        per_upload = QUOTA_COSTS.get("video_upload", 1600) + \
            QUOTA_COSTS.get("thumbnail_set", 50)  # each video usually sets a thumb
        estimate = yt_uploads * per_upload
        used = int(get_quota_used() or 0)
        remaining = max(0, int(DAILY_QUOTA) - used)
        fits = estimate <= remaining
        # How many dates' worth of YouTube fit per day (Video+Shorts = 2 uploads).
        per_day_dates = int(DAILY_QUOTA // (2 * per_upload)) if per_upload else 0
        msg = ""
        if yt_uploads and not fits:
            msg = (f"This run needs ~{estimate:,} YouTube quota units but only "
                   f"~{remaining:,} remain today (cap {int(DAILY_QUOTA):,}). "
                   f"YouTube uploads past the cap fail with 'quotaExceeded'. "
                   f"Only about {per_day_dates} date(s) of YouTube (Video+Shorts) "
                   f"fit per day — spread the run across days, upload the "
                   f"non-YouTube platforms now, or request a quota increase.")
        return {
            "youtube_uploads": yt_uploads,
            "estimate_units": estimate,
            "remaining_units": remaining,
            "cap_units": int(DAILY_QUOTA),
            "fits": bool(fits),
            "dates_per_day": per_day_dates,
            "message": msg,
        }
    except Exception as e:  # noqa: BLE001 — estimate is best-effort
        return {"youtube_uploads": yt_uploads, "fits": True, "message": "",
                "error": str(e)}


@bp.route("/preflight/dryrun", methods=["POST"])
def preflight_dryrun():
    """Per-(date, platform) data validation for the selected run.

    Body JSON:
      * ``dates`` — iso dates to validate.
      * ``platforms`` — platform keys enabled for the run.
      * ``scan`` — the browser's scan result map (``{iso: {categories, metadata}}``).
    """
    data = request.get_json(silent=True) or {}
    result = validate_run(
        dates=data.get("dates") or [],
        platforms=data.get("platforms") or [],
        scan=data.get("scan") or {},
    )
    return jsonify(result), 200


@bp.route("/preflight/check", methods=["GET"])
def preflight_check():
    """Per-platform readiness verdict for the dashboard's pre-run check.

    Query params:
      * ``platforms`` — optional CSV of platform keys to limit the check
        (e.g. ``?platforms=youtube_video,rock``). Default: all.
      * ``probe`` — ``1`` to add a live Unsplash search (costs one API call).
    """
    raw = (request.args.get("platforms") or "").strip()
    platforms = [p.strip() for p in raw.split(",") if p.strip()] or None
    probe = request.args.get("probe") in ("1", "true", "yes")
    result = run_preflight(platforms=platforms, probe=probe)
    # Always 200: the body carries the verdict. A non-200 would make the
    # dashboard's fetch error-handling swallow the useful per-platform detail.
    return jsonify(result), 200
