"""Settings page, OAuth, llamafile status, Excel mapping, env-file editing."""
from __future__ import annotations

import json
import logging
import os

import yaml
from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import core.config as core_config
from core.hosted import is_hosted
from core.org_context import forbidden_during_impersonation
from core.permissions import is_program_owner as _is_program_owner

_log = logging.getLogger(__name__)

# Canonical secret slots surfaced on the Settings → Secrets panel. Values are
# never shown; "text" secrets get an overwrite-only input, "managed" secrets are
# set through their own flow (status + where-to-set only).
KNOWN_SECRETS = [
    {"name": "PEXELS_API_KEY", "label": "Pexels API key", "required": False, "kind": "text"},
    {"name": "UNSPLASH_ACCESS_KEY", "label": "Unsplash access key", "required": False, "kind": "text"},
    {"name": "youtube.client_secrets", "label": "YouTube client_secrets.json", "required": True, "kind": "managed", "where": "API Credentials → upload client_secrets"},
    {"name": "youtube.token", "label": "YouTube OAuth token", "required": True, "kind": "managed", "where": "API Credentials → Re-authenticate"},
    {"name": "playwright.simplecast_session", "label": "SimpleCast session", "required": False, "kind": "managed", "where": "API Credentials → Connect"},
    {"name": "playwright.vista_social_session", "label": "Vista Social session", "required": False, "kind": "managed", "where": "API Credentials → Connect"},
    {"name": "playwright.rock_session", "label": "Rock session", "required": False, "kind": "managed", "where": "API Credentials → Connect"},
]
from core.config import (
    ENV_PATH,
    PROJECT_ROOT,
    invalidate_config_cache,
    load_config,
)
from core.excel_parser import ExcelParser
from core.session_state import session
from core.quota import DAILY_QUOTA, get_quota_used
from uploaders.youtube_uploader import (
    get_authenticated_service,
)
from core.yt_auth_cache import (
    cached_yt_authenticated as _cached_yt_authenticated,
    invalidate_yt_auth_cache,
)

bp = Blueprint("settings", __name__)


def _read_env_file() -> dict:
    """Read .env file and return key-value pairs."""
    values = {}
    if not os.path.exists(ENV_PATH):
        return values
    # M11: a corrupt or transiently-locked .env used to 500 the Settings
    # GET (and any route that reads env via this helper). Surface a clear
    # warning and return what we have rather than crash the page.
    try:
        with open(ENV_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    values[key.strip()] = val.strip()
    except OSError as e:
        # Module logger (not current_app.logger) so this works outside a
        # request context and never needs its own swallow guard.
        _log.warning("Failed to read .env at %s: %s", ENV_PATH, e)
    return values


def _write_env_file(values: dict) -> None:
    """Write key-value pairs to .env, preserving keys not in *values*.

    Rejects values containing newlines so a pasted multi-line secret cannot
    inject new env vars. Quotes values containing whitespace or `=` so
    python-dotenv reads them back as a single value.
    """
    existing = _read_env_file()
    existing.update(values)
    # M12 caller: caller is responsible for catching OSError; we leave the
    # raise in place so the calling route can flash a user-facing message.
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        for key, val in existing.items():
            sval = "" if val is None else str(val)
            if "\n" in sval or "\r" in sval:
                raise ValueError(f"env value for {key!r} contains newline")
            if any(c in sval for c in (" ", "\t", "=", "#", '"', "'")):
                escaped = sval.replace("\\", "\\\\").replace('"', '\\"')
                line = f'{key}="{escaped}"\n'
            else:
                line = f"{key}={sval}\n"
            f.write(line)


def _mask_secret(value: str) -> str:
    """Return masked version of a secret, showing only last 4 chars."""
    if not value or len(value) <= 4:
        return ""
    return "•" * (len(value) - 4) + value[-4:]


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    """Settings page: view and update config.yaml and .env values."""
    if request.method == "POST":
        config = load_config()

        # Per-org sections (scheduling + description_footers) write to the
        # org_settings overlay, NOT to config.yaml. config.yaml is the
        # platform default; per-tenant overrides live in SQLite so each org
        # (and the program owner while impersonating) gets their own values.
        from core.org_context import effective_org_id
        from core import org_settings as _org_settings
        target_org_id = effective_org_id()

        sched_overlay = {
            "youtube_video":  request.form.get("sched_youtube_video", "10:00"),
            "youtube_shorts": request.form.get("sched_youtube_shorts", "12:00"),
            "simplecast":     request.form.get("sched_simplecast", "06:00"),
            "vista_social":   request.form.get("sched_vista_social", "12:00"),
            "timezone":       request.form.get("sched_timezone", "America/New_York"),
        }
        footer_overlay = {
            "youtube_video":  request.form.get("footer_youtube_video", ""),
            "youtube_shorts": request.form.get("footer_youtube_shorts", ""),
            "podcast":        request.form.get("footer_podcast", ""),
            "vista_social":   request.form.get("footer_vista_social", ""),
        }
        if target_org_id is not None:
            _org_settings.set_section(target_org_id, "scheduling", sched_overlay)
            _org_settings.set_section(target_org_id, "description_footers", footer_overlay)
        else:
            # Legacy single-tenant install (LEGACY_PASSWORD_ENABLED, no
            # current_org_id in session): keep writing to config.yaml so the
            # USB build's existing behavior is preserved.
            config.setdefault("scheduling", {}).update(sched_overlay)
            config.setdefault("description_footers", {}).update(footer_overlay)

        config.setdefault("youtube", {})
        config["youtube"]["default_privacy"] = request.form.get("yt_default_privacy", "private")
        config["youtube"]["default_category_id"] = request.form.get("yt_category_id", "22")
        config["youtube"]["made_for_kids"] = "yt_made_for_kids" in request.form

        config.setdefault("simplecast", {})
        try:
            config["simplecast"]["default_season"] = int(request.form.get("sc_default_season", 1))
        except (ValueError, TypeError):
            config["simplecast"]["default_season"] = 1
        config["simplecast"]["explicit"] = "sc_explicit" in request.form

        config.setdefault("llm", {})
        config["llm"]["model"] = request.form.get("llm_model", "llama3.2")
        try:
            config["llm"]["num_title_suggestions"] = int(request.form.get("llm_num_titles", 5))
        except (ValueError, TypeError):
            config["llm"]["num_title_suggestions"] = 5

        # Media folders + the planning spreadsheet are picked per browser
        # session on the dashboard now — no server-side directory config.

        # M12: a full or read-only USB used to 500 the Settings POST. Catch
        # write failures and surface them as flash messages instead.
        try:
            with open(core_config.CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.safe_dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        except OSError as e:
            flash(f"Could not save config.yaml ({e}). Check that the drive is writable.", "danger")
            return redirect(url_for("settings.settings"))
        invalidate_config_cache()
        ExcelParser(load_config()).invalidate_cache()
        session.reload_config()

        # client_secrets is platform-shared (every tenant auths through the
        # program owner's GCP project). Only program-owners can upload it;
        # org users don't see the row, but a hand-crafted POST also fails here.
        from core.org_context import real_user_id
        uid = real_user_id()
        client_secrets_file = request.files.get("youtube_client_secrets")
        if client_secrets_file and client_secrets_file.filename:
            if uid is None or not _is_program_owner(uid):
                abort(403)
            blob = client_secrets_file.read(256 * 1024 + 1)
            if len(blob) > 256 * 1024:
                flash("client_secrets.json too large (>256 KB).", "danger")
                return redirect(url_for("settings.settings"))
            try:
                parsed = json.loads(blob)
            except (UnicodeDecodeError, json.JSONDecodeError):
                flash("client_secrets.json is not valid JSON.", "danger")
                return redirect(url_for("settings.settings"))
            if "installed" not in parsed and "web" not in parsed:
                flash("client_secrets.json missing 'installed' or 'web' key — "
                      "is this an OAuth client secrets file?", "danger")
                return redirect(url_for("settings.settings"))
            # Keep the on-disk copy for the legacy single-tenant USB path
            # (LEGACY_PASSWORD_ENABLED). Multi-tenant production reads from the
            # platform store.
            dest = os.path.join(PROJECT_ROOT, "client_secrets.json")
            try:
                with open(dest, "wb") as f:
                    f.write(blob)
            except OSError as e:
                flash(f"Could not save client_secrets.json ({e}).", "danger")
                return redirect(url_for("settings.settings"))
            from core import secrets_store
            secrets_store.set_platform_blob("youtube.client_secrets", blob)

        flash("Settings saved successfully!", "success")
        return redirect(url_for("settings.settings"))

    from core.org_context import real_user_id, effective_org_id
    from core.config import effective_config as _effective_config
    # Per-org overlay applied: the schedule/footer fields in the form
    # render the active org's overrides (with config.yaml as the fallback).
    config = _effective_config(effective_org_id())
    from core import secrets_store as _ss
    uid = real_user_id()
    is_program_owner = uid is not None and _is_program_owner(uid)

    secrets_path = os.path.join(PROJECT_ROOT, "client_secrets.json")
    # Check the encrypted store too, not just disk: the upload persists the blob
    # to the store (which survives redeploys), but the on-disk copy lives on the
    # ephemeral container fs. Disk-only made it look "lost" after every redeploy
    # even though the secret was safe — match how sessions report presence.
    client_secrets_found = (
        os.path.isfile(secrets_path) or _ss.has_platform_secret("youtube.client_secrets")
    )
    from core.playwright_session import has_session
    org_id = effective_org_id()
    simplecast_session_found = has_session(
        os.path.join(PROJECT_ROOT, "simplecast_session.json"), org_id=org_id,
    )
    vista_social_session_found = has_session(
        os.path.join(PROJECT_ROOT, "vista_social_session.json"), org_id=org_id,
    )
    rock_session_found = has_session(
        os.path.join(PROJECT_ROOT, "rock_session.json"), org_id=org_id,
    )

    # Intentionally do NOT pass raw .env values to the template — they
    # contain API keys and would leak through the rendered HTML to any
    # browser tab that can reach this page. The template only needs
    # presence/absence indicators, which it derives from the *_found flags
    # and from `config` above.
    from core import secrets_store
    from core.auth import _HASH_SECRET
    # Platform-scoped secrets (youtube.client_secrets) are only visible and
    # manageable by program owners.  Per-org users see only their org's keys.
    _platform_only = {"youtube.client_secrets"}
    known_secrets = [
        {**spec, "is_set": secrets_store.has_secret(spec["name"], org_id=org_id)}
        for spec in KNOWN_SECRETS
        if spec["name"] not in _platform_only or is_program_owner
    ]
    known_names = {spec["name"] for spec in KNOWN_SECRETS}
    # Anything stored that isn't a known slot (e.g. a manually-added key) —
    # surface it too so nothing is hidden, with overwrite + clear.
    extra_secrets = [n for n in secrets_store.list_secret_names(org_id=org_id)
                     if n != _HASH_SECRET and n not in known_names]
    return render_template(
        "settings.html",
        is_program_owner=is_program_owner,
        config=config,
        client_secrets_found=client_secrets_found,
        simplecast_session_found=simplecast_session_found,
        vista_social_session_found=vista_social_session_found,
        rock_session_found=rock_session_found,
        youtube_authenticated=_cached_yt_authenticated(),
        known_secrets=known_secrets,
        extra_secrets=extra_secrets,
        hosted=is_hosted(),
    )


@bp.route("/settings/clear-simplecast-session", methods=["POST"])
def clear_simplecast_session():
    """Clear the saved SimpleCast session (store + disk) for the active org."""
    from core.playwright_session import has_session, clear_session
    from core.org_context import effective_org_id
    oid = effective_org_id()
    sess_path = os.path.join(PROJECT_ROOT, "simplecast_session.json")
    if not has_session(sess_path, org_id=oid):
        flash("No SimpleCast session found.", "warning")
        return redirect(url_for("settings.settings"))
    try:
        clear_session(sess_path, org_id=oid)
        flash("SimpleCast session cleared.", "success")
    except OSError as e:
        flash(f"Could not clear SimpleCast session ({e}). Close any open Chrome windows and try again.", "danger")
    return redirect(url_for("settings.settings"))


@bp.route("/settings/clear-vista-social-session", methods=["POST"])
def clear_vista_social_session():
    """Clear the saved Vista Social session (store + disk) for the active org."""
    from core.playwright_session import has_session, clear_session
    from core.org_context import effective_org_id
    oid = effective_org_id()
    sess_path = os.path.join(PROJECT_ROOT, "vista_social_session.json")
    if not has_session(sess_path, org_id=oid):
        flash("No Vista Social session found.", "warning")
        return redirect(url_for("settings.settings"))
    try:
        clear_session(sess_path, org_id=oid)
        flash("Vista Social session cleared.", "success")
    except OSError as e:
        flash(f"Could not clear Vista Social session ({e}). Close any open Chrome windows and try again.", "danger")
    return redirect(url_for("settings.settings"))


def _run_browser_login(name: str, build_config) -> None:
    """Open a Playwright session for *name*, triggering manual login if needed.

    Used by the per-service Login buttons in Settings (local only): opening the
    PlaywrightSession context manager runs `_handle_login` automatically when
    no session file is present (or when the saved session redirects to a
    login page), and the storage_state is persisted on context exit.
    """
    from core.playwright_session import PlaywrightSession
    cfg = build_config()
    with PlaywrightSession(cfg):
        pass


def _hosted_login_redirect():
    """On the headless hosted instance, the local-Chrome login can't work —
    point the user at the streamed Connect panel instead of erroring."""
    flash(
        "This is the hosted instance — use the “Connect a browser platform” "
        "panel in Settings to sign in through the server-hosted browser.",
        "info",
    )
    return redirect(url_for("settings.settings"))


@bp.route("/settings/login-simplecast", methods=["POST"])
def login_simplecast():
    """Open Chrome and walk the user through SimpleCast login."""
    if is_hosted():
        return _hosted_login_redirect()
    def _build():
        from dataclasses import replace
        from uploaders.simplecast_uploader import (
            _SC_SESSION_CONFIG_BASE, _resolve_upload_url,
        )
        return replace(_SC_SESSION_CONFIG_BASE, target_url=_resolve_upload_url())
    try:
        _run_browser_login("simplecast", _build)
        flash("SimpleCast login saved.", "success")
    except Exception as e:
        flash(f"SimpleCast login failed: {e}", "danger")
    return redirect(url_for("settings.settings"))


@bp.route("/settings/login-vista-social", methods=["POST"])
def login_vista_social():
    """Open Chrome and walk the user through Vista Social login."""
    if is_hosted():
        return _hosted_login_redirect()
    def _build():
        from uploaders.vista_social_uploader import _VS_SESSION_CONFIG
        return _VS_SESSION_CONFIG
    try:
        _run_browser_login("vista_social", _build)
        flash("Vista Social login saved.", "success")
    except Exception as e:
        flash(f"Vista Social login failed: {e}", "danger")
    return redirect(url_for("settings.settings"))


@bp.route("/settings/login-rock", methods=["POST"])
def login_rock():
    """Open Chrome and walk the user through Rock RMS login."""
    if is_hosted():
        return _hosted_login_redirect()
    def _build():
        from uploaders.rock.client import _ROCK_SESSION_CONFIG
        return _ROCK_SESSION_CONFIG
    try:
        _run_browser_login("rock", _build)
        flash("Rock login saved.", "success")
    except Exception as e:
        flash(f"Rock login failed: {e}", "danger")
    return redirect(url_for("settings.settings"))


@bp.route("/settings/clear-rock-session", methods=["POST"])
def clear_rock_session():
    """Clear the saved Rock session (store + disk) for the active org."""
    from core.playwright_session import has_session, clear_session
    from core.org_context import effective_org_id
    oid = effective_org_id()
    sess_path = os.path.join(PROJECT_ROOT, "rock_session.json")
    if not has_session(sess_path, org_id=oid):
        flash("No Rock session found.", "warning")
        return redirect(url_for("settings.settings"))
    try:
        clear_session(sess_path, org_id=oid)
        flash("Rock session cleared.", "success")
    except OSError as e:
        flash(f"Could not clear Rock session ({e}). Close any open Chrome windows and try again.", "danger")
    return redirect(url_for("settings.settings"))


def _hosted_youtube_oauth_redirect():
    """Hosted mode: send the user's own browser to Google's consent screen and
    stash a CSRF state for the callback. The desktop loopback flow can't run on
    a headless server, so this is the only path that works there."""
    from flask import session as flask_session
    from uploaders.youtube_uploader import start_web_authorization
    redirect_uri = url_for("settings.oauth_youtube_callback",
                           _external=True, _scheme="https")
    try:
        auth_url, state, code_verifier = start_web_authorization(redirect_uri)
    except Exception as e:
        flash(f"YouTube authentication failed: {e}", "danger")
        return redirect(url_for("settings.settings"))
    flask_session["yt_oauth_state"] = state
    # PKCE verifier must survive to the callback and be replayed at token
    # exchange, or Google rejects it with "Missing code verifier".
    flask_session["yt_oauth_code_verifier"] = code_verifier
    return redirect(auth_url)


@bp.route("/oauth/youtube", methods=["POST"])
def oauth_youtube():
    """Trigger YouTube OAuth2 flow."""
    if is_hosted():
        return _hosted_youtube_oauth_redirect()
    try:
        get_authenticated_service()
        invalidate_yt_auth_cache()
        flash("YouTube authentication successful!", "success")
    except FileNotFoundError as e:
        flash(str(e), "danger")
    except Exception as e:
        flash(f"YouTube authentication failed: {str(e)}", "danger")
    return redirect(url_for("scan.dashboard"))


@bp.route("/oauth/youtube/settings", methods=["POST"])
def oauth_youtube_settings():
    """Trigger YouTube OAuth2 flow and redirect back to settings."""
    if is_hosted():
        return _hosted_youtube_oauth_redirect()
    try:
        get_authenticated_service()
        invalidate_yt_auth_cache()
        flash("YouTube authentication successful!", "success")
    except FileNotFoundError as e:
        flash(str(e), "danger")
    except Exception as e:
        flash(f"YouTube authentication failed: {str(e)}", "danger")
    return redirect(url_for("settings.settings"))


@bp.route("/oauth/youtube/callback")
def oauth_youtube_callback():
    """Google redirects the user's browser here after consent (hosted web
    flow). Verify the CSRF state, exchange the code for a token, and store it.
    """
    from flask import session as flask_session
    from uploaders.youtube_uploader import finish_web_authorization

    err = request.args.get("error")
    if err:
        flash(f"YouTube sign-in was cancelled or failed: {err}", "danger")
        return redirect(url_for("settings.settings"))

    expected_state = flask_session.pop("yt_oauth_state", None)
    code_verifier = flask_session.pop("yt_oauth_code_verifier", None)
    if not expected_state or expected_state != request.args.get("state"):
        flash("YouTube sign-in could not be verified (state mismatch). "
              "Please click Connect YouTube and try again.", "danger")
        return redirect(url_for("settings.settings"))

    # Rebuild the authorization-response URL from the public https callback so
    # the internal http hop behind Cloudflare doesn't trip oauthlib's transport
    # check, and so it matches the registered redirect URI exactly.
    redirect_uri = url_for("settings.oauth_youtube_callback",
                           _external=True, _scheme="https")
    auth_response = redirect_uri
    if request.query_string:
        auth_response += "?" + request.query_string.decode("utf-8")

    try:
        finish_web_authorization(redirect_uri, expected_state, code_verifier,
                                 auth_response)
        invalidate_yt_auth_cache()   # flip the Settings badge immediately
        flash("YouTube authentication successful!", "success")
    except Exception as e:
        flash(f"YouTube authentication failed: {e}", "danger")
    return redirect(url_for("settings.settings"))


@bp.route("/settings/clear-youtube-token", methods=["POST"])
def clear_youtube_token():
    """Clear the stored YouTube OAuth token and redirect back to settings."""
    from uploaders.youtube_uploader import _clear_token, _YT_TOKEN_NAME
    from core import secrets_store
    from core.org_context import effective_org_id
    if secrets_store.has_secret(_YT_TOKEN_NAME, org_id=effective_org_id()):
        _clear_token()
        invalidate_yt_auth_cache()
        flash("YouTube token cleared.", "success")
    else:
        flash("No YouTube token was set.", "warning")
    return redirect(url_for("settings.settings"))


@bp.route("/youtube/status")
def youtube_status():
    """Return YouTube auth/token status and session-based quota estimate."""
    token_valid = _cached_yt_authenticated()
    quota_used = get_quota_used()
    return jsonify(
        {
            "token_valid": token_valid,
            "quota_limit": DAILY_QUOTA,
            "quota_used_this_session": quota_used,
            "quota_remaining_estimate": DAILY_QUOTA - quota_used,
            "note": "Persistent daily counter; resets at midnight Pacific.",
        }
    )


@bp.route("/sessions/status")
def sessions_status():
    """Single endpoint for the sidebar status panel.

    Returns a flat dict {name: {ok, label}} for every connection the
    sidebar needs to render. One round-trip beats 4–5 separate fetches
    on every page load.

    Auth: session cookie OR a valid agent pair token (?token=<...>).
    The agent's hero-status GUI polls this so its session-rows panel
    mirrors what the website's sidebar shows; without the token path
    the agent (which has no cookie) would always see 'unknown'.
    """
    # Inline auth: this endpoint is listed in _PUBLIC_ENDPOINTS so the
    # global before_request hook doesn't redirect to /login — we
    # enforce here so both browser cookies and agent tokens work.
    from flask import abort, request as _req, session as _sess
    has_session_auth = bool(_sess.get("user_id") or _sess.get("authenticated"))
    # Track which device authenticated us via token — needed below to
    # resolve which org the agent's user is currently acting as. None
    # means "this is a browser request, not the agent."
    auth_device_id: str | None = None
    if not has_session_auth:
        tok = (_req.args.get("token") or "").strip()
        if not tok:
            abort(401)
        try:
            from core.devices import verify_device_token
            auth_device_id = verify_device_token(tok)
            if not auth_device_id:
                abort(401)
        except Exception:
            abort(401)
    from core.playwright_session import has_session
    from core.llm_title_gen import is_llamafile_running
    from core.org_context import effective_org_id
    # Resolve the effective org. Browser path: use the session overlay
    # (effective_org_id). Agent path: read the device owner's
    # users.acting_as_org_id (mirrored by impersonation.start/end) so the
    # agent's status panel reflects the user's current impersonation;
    # fall back to one of the user's memberships when not impersonating.
    if has_session_auth:
        org_id = effective_org_id()
    else:
        org_id = None
        try:
            from core import devices as _devs, db as _db_mod
            from core import org_store as _ostore
            owner_uid = _devs.get_device_owner(auth_device_id) if auth_device_id else None
            if owner_uid is not None:
                with _db_mod._get_conn() as _c:
                    row = _c.execute(
                        "SELECT acting_as_org_id FROM users WHERE id = ?",
                        (int(owner_uid),),
                    ).fetchone()
                acting = row["acting_as_org_id"] if row else None
                if acting is not None:
                    org_id = int(acting)
                else:
                    mems = _ostore.list_memberships_for_user(int(owner_uid))
                    if mems:
                        org_id = int(mems[0]["org_id"])
        except Exception:
            org_id = None
    out: dict = {}
    # Agent (token-auth) requests have no Flask session, so
    # _cached_yt_authenticated() — which reads effective_org_id() from
    # the (empty) session — would always miss and report "needs auth"
    # even when the token sits in the agent owner's org. Push the
    # resolved org_id as a thread-local override for this one call so
    # the YT auth check reads the right tenant's slot. Browser path
    # (has_session_auth=True) doesn't need this — its session already
    # has acting_as_org_id / current_org_id and effective_org_id()
    # resolves correctly.
    if has_session_auth:
        yt_ok = bool(_cached_yt_authenticated())
    else:
        from core.org_context import override as _oc_override
        with _oc_override(org_id):
            yt_ok = bool(_cached_yt_authenticated())
    out["youtube"] = {
        "ok": yt_ok,
        "label_on": "YouTube connected",
        "label_off": "YouTube needs auth",
    }
    out["simplecast"] = {
        "ok": bool(has_session(os.path.join(PROJECT_ROOT, "simplecast_session.json"), org_id=org_id)),
        "label_on": "SimpleCast session",
        "label_off": "SimpleCast needs login",
    }
    out["vista_social"] = {
        "ok": bool(has_session(os.path.join(PROJECT_ROOT, "vista_social_session.json"), org_id=org_id)),
        "label_on": "Vista Social session",
        "label_off": "Vista Social needs login",
    }
    out["rock"] = {
        "ok": bool(has_session(os.path.join(PROJECT_ROOT, "rock_session.json"), org_id=org_id)),
        "label_on": "Rock session",
        "label_off": "Rock needs login",
    }
    # Agent online — read from the process-wide relay registered by
    # blueprints.agent at startup, then filtered to the device pool the
    # current request would actually dispatch to. Without the filter the
    # sidebar reports "Agent online" when the only online agent belongs
    # to a different tenant — misleading, since clicking Upload would
    # refuse to dispatch to it.
    #
    # Browser path: _eligible_device_ids reads from the Flask session
    # (effective_org_id + impersonation). Agent path: the session is
    # empty, so we compute eligibility from the resolved owner-side
    # org_id above (matches what a dispatch from the impersonating
    # browser would pick).
    try:
        from core.relay import _default_relay, _default_account
        if has_session_auth:
            from core import agent_dispatch as _ad
            eligible = _ad._eligible_device_ids()
        else:
            from core import devices as _devs
            eligible = (
                _devs.list_device_ids_in_org(org_id) if org_id is not None else None
            )
        if _default_relay is None:
            agent_count = 0
        else:
            online = _default_relay.online_agents(_default_account)
            if eligible is None:
                agent_count = len(online)
            else:
                agent_count = sum(1 for a in online if a["device_id"] in eligible)
    except Exception:
        agent_count = 0
    out["agent"] = {
        "ok": agent_count > 0,
        "label_on": f"Agent online ({agent_count})" if agent_count > 1 else "Agent online",
        "label_off": "No agent connected",
    }
    out["ollama"] = {
        "ok": bool(is_llamafile_running()),
        "label_on": "Ollama connected",
        "label_off": "Ollama offline",
    }
    # Paired-device count for the current user (not just currently-online).
    # The dashboard's empty-state download card uses this to hide itself
    # the moment a fresh pairing lands, without a page reload.
    paired_count = 0
    try:
        from flask import session as _sess
        from core import devices as _devices
        uid = _sess.get("user_id")
        if uid is not None:
            paired_count = int(_devices.count_user_devices(int(uid)))
    except Exception:
        paired_count = 0
    out["_meta"] = {"paired_device_count": paired_count}
    return jsonify(out)


@bp.route("/llamafile/status")
def llamafile_status():
    """Return the LLM (title-generation) backend status.

    Checks the *configured* endpoint (LLM_BASE_URL — Ollama on the hosted VPS,
    a local llamafile elsewhere), not a hardcoded port, so the Settings panel
    matches /health and reality.
    """
    from core.llm_title_gen import is_llamafile_running, LLM_BASE_URL, LLM_MODEL
    return jsonify({
        "running": bool(is_llamafile_running()),
        "model": LLM_MODEL,
        "url": LLM_BASE_URL,
    })


@bp.route("/settings/set-secret", methods=["POST"])
def set_secret_route():
    name = (request.form.get("name") or "").strip()
    value = request.form.get("value") or ""
    if not (name and value):
        flash("Secret name and value are both required.", "warning")
        return redirect(url_for("settings.settings"))
    try:
        from core import secrets_store
        from core.org_context import effective_org_id
        secrets_store.set_secret(name, value, org_id=effective_org_id())
        flash(f"Secret '{name}' saved.", "success")
    except Exception as e:
        flash(f"Could not save secret: {e}", "danger")
    return redirect(url_for("settings.settings"))


@bp.route("/settings/clear-secret", methods=["POST"])
def clear_secret_route():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("No secret specified.", "warning")
        return redirect(url_for("settings.settings"))
    try:
        from core import secrets_store
        from core.org_context import effective_org_id
        secrets_store.delete_secret(name, org_id=effective_org_id())
        flash(f"Secret '{name}' cleared.", "success")
    except Exception as e:
        flash(f"Could not clear secret: {e}", "danger")
    return redirect(url_for("settings.settings"))


@bp.route("/settings/devices", methods=["GET"])
def devices_page():
    """Manage paired hybrid-agent devices.

    Lists every device (active + revoked). The agent ws-relay endpoints
    are HYBRID_AGENT_ENABLED-gated, but reading the device table is safe
    even when the feature is off — we just render an empty list.
    The rename/revoke action endpoints live under the agent blueprint
    (and are therefore only useful when HYBRID is enabled, which is the
    only time there can be devices to manage).
    """
    from core import devices as _devices
    from core.org_context import real_user_id
    from flask import session as _sess
    # SEC-001: scope to the caller's own devices. list_devices() is the
    # system-wide admin view; rendering it here leaked every other tenant's
    # device inventory (id, name, hostname, hwid_hash) to any authenticated
    # user. Mirror GET /agent/devices: program-owner / legacy single-tenant
    # session sees all; everyone else sees only the devices they own.
    uid = real_user_id()
    legacy = bool(_sess.get("authenticated") and _sess.get("user_id") is None)
    is_owner = uid is not None and _is_program_owner(uid)
    if legacy or is_owner:
        device_list = _devices.list_devices()
    elif uid is not None:
        device_list = _devices.list_devices_for_user(uid)
    else:
        device_list = []
    return render_template(
        "devices.html",
        devices=device_list,
        hybrid_enabled=os.environ.get("HYBRID_AGENT_ENABLED", "").lower()
            in ("1", "true", "yes"),
        device_name_max_len=_devices.DEVICE_NAME_MAX_LEN,
    )


@bp.route("/settings/change-password", methods=["POST"])
@forbidden_during_impersonation
def change_password_route():
    current = request.form.get("current") or ""
    new = request.form.get("new") or ""
    if not new or not current:
        flash("Both current and new password are required.", "warning")
        return redirect(url_for("settings.settings"))
    try:
        from core import auth
        if not auth.verify_password(current):
            flash("Could not change password (current password is incorrect).", "danger")
            return redirect(url_for("settings.settings"))
        auth.set_password(new)
        flash("Password changed.", "success")
    except Exception as e:
        flash(f"Could not change password: {e}", "danger")
    return redirect(url_for("settings.settings"))


# ---------- Phase γ: security preferences + org-level Require-2FA ----------

@bp.route("/settings/security", methods=["GET"])
def security_get():
    from flask import session as _sess
    from core import db as _db
    uid = _sess.get("user_id")
    if uid is None:
        return redirect(url_for("auth.login"))
    user = _db.get_user_by_id(uid)
    notify = bool(user.get("notify_new_device", 1)) if user else True
    require_2fa = False
    try:
        from core import org_store as _os
        from core import auth as _auth
        oid = _auth.current_org_id()
        if oid is not None:
            org = _os.get_org_by_id(oid)
            require_2fa = bool((org or {}).get("require_2fa"))
    except Exception:
        require_2fa = False
    return render_template(
        "settings_security.html",
        notify_new_device=notify,
        require_2fa=require_2fa,
    )


@bp.route("/settings/security", methods=["POST"])
def security_post():
    from flask import session as _sess
    from core import db as _db
    uid = _sess.get("user_id")
    if uid is None:
        return redirect(url_for("auth.login"))
    on = bool(request.form.get("notify_new_device"))
    _db.set_user_notify_new_device(uid, on)
    return redirect(url_for("settings.security_get"))


@bp.route("/settings/org/require-2fa", methods=["POST"])
def org_require_2fa():
    from flask import session as _sess
    from core import audit as _audit
    from core import db as _db
    uid = _sess.get("user_id")
    org_id = _sess.get("current_org_id")
    if uid is None:
        return redirect(url_for("auth.login"))
    if not org_id:
        return ("No org selected", 400)
    m = _db.get_membership(uid, org_id)
    if not m or m["role"] != "owner":
        return ("Forbidden", 403)
    org = _db.get_org(org_id) or {}
    before = bool(org.get("require_2fa"))
    enabled = bool(request.form.get("enabled"))
    _db.set_org_require_2fa(org_id, enabled)
    _audit.write_event(
        action="org.settings_changed",
        actor_user_id=uid, org_id=org_id,
        target_type="org", target_id=org_id,
        metadata={"changes": {"require_2fa": [before, enabled]}},
    )
    # Land back on /settings/security since that's where the operator
    # most likely came from. Validate the referrer through auth._safe_next
    # so a forged Referer can't turn this into an open redirect.
    from blueprints.auth import _safe_referrer_redirect
    return _safe_referrer_redirect("settings.security_get")
