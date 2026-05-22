"""Settings page, OAuth, llamafile status, Excel mapping, env-file editing."""
from __future__ import annotations

import json
import logging
import os

import yaml
from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import core.config as core_config
from core.hosted import is_hosted

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
from app import _cached_yt_authenticated

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

        config.setdefault("scheduling", {})
        config["scheduling"]["youtube_video"] = request.form.get("sched_youtube_video", "10:00")
        config["scheduling"]["youtube_shorts"] = request.form.get("sched_youtube_shorts", "12:00")
        config["scheduling"]["simplecast"] = request.form.get("sched_simplecast", "06:00")
        config["scheduling"]["vista_social"] = request.form.get("sched_vista_social", "12:00")
        config["scheduling"]["timezone"] = request.form.get("sched_timezone", "America/New_York")

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

        config.setdefault("description_footers", {})
        config["description_footers"]["youtube_video"] = request.form.get("footer_youtube_video", "")
        config["description_footers"]["youtube_shorts"] = request.form.get("footer_youtube_shorts", "")
        config["description_footers"]["podcast"] = request.form.get("footer_podcast", "")
        config["description_footers"]["vista_social"] = request.form.get("footer_vista_social", "")

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

        uploaded = request.files.get("client_secrets_file")
        if uploaded and uploaded.filename:
            # Cap size, parse JSON, and require an OAuth installed/web client
            # block. Otherwise a stray binary upload silently overwrites a
            # valid secrets file, and the next OAuth attempt fails with a
            # confusing JSON parse error far away from the upload.
            _MAX_SECRETS_BYTES = 256 * 1024
            blob = uploaded.read(_MAX_SECRETS_BYTES + 1)
            if len(blob) > _MAX_SECRETS_BYTES:
                flash("client_secrets.json too large (>256 KB).", "danger")
                return redirect(url_for("settings.settings"))
            try:
                parsed = json.loads(blob.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                flash(f"client_secrets.json is not valid JSON: {e}", "danger")
                return redirect(url_for("settings.settings"))
            if not isinstance(parsed, dict) or not (
                "installed" in parsed or "web" in parsed
            ):
                flash(
                    "client_secrets.json missing 'installed' or 'web' key — "
                    "is this an OAuth client secrets file?",
                    "danger",
                )
                return redirect(url_for("settings.settings"))
            dest = os.path.join(PROJECT_ROOT, "client_secrets.json")
            try:
                with open(dest, "wb") as f:
                    f.write(blob)
            except OSError as e:
                flash(f"Could not save client_secrets.json ({e}).", "danger")
                return redirect(url_for("settings.settings"))
            from core import secrets_store
            secrets_store.set_blob("youtube.client_secrets", blob)

        flash("Settings saved successfully!", "success")
        return redirect(url_for("settings.settings"))

    config = load_config()

    secrets_path = os.path.join(PROJECT_ROOT, "client_secrets.json")
    # Check the encrypted store too, not just disk: the upload persists the blob
    # to the store (which survives redeploys), but the on-disk copy lives on the
    # ephemeral container fs. Disk-only made it look "lost" after every redeploy
    # even though the secret was safe — match how sessions report presence.
    from core import secrets_store as _ss_cs
    client_secrets_found = (
        os.path.isfile(secrets_path) or _ss_cs.has_secret("youtube.client_secrets")
    )
    from core.playwright_session import has_session
    simplecast_session_found = has_session(
        os.path.join(PROJECT_ROOT, "simplecast_session.json")
    )
    vista_social_session_found = has_session(
        os.path.join(PROJECT_ROOT, "vista_social_session.json")
    )
    rock_session_found = has_session(
        os.path.join(PROJECT_ROOT, "rock_session.json")
    )

    # Intentionally do NOT pass raw .env values to the template — they
    # contain API keys and would leak through the rendered HTML to any
    # browser tab that can reach this page. The template only needs
    # presence/absence indicators, which it derives from the *_found flags
    # and from `config` above.
    from core import secrets_store
    from core.auth import _HASH_SECRET
    known_secrets = [
        {**spec, "is_set": secrets_store.has_secret(spec["name"])}
        for spec in KNOWN_SECRETS
    ]
    known_names = {spec["name"] for spec in KNOWN_SECRETS}
    # Anything stored that isn't a known slot (e.g. a manually-added key) —
    # surface it too so nothing is hidden, with overwrite + clear.
    extra_secrets = [n for n in secrets_store.list_secret_names()
                     if n != _HASH_SECRET and n not in known_names]
    return render_template(
        "settings.html",
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
    """Clear the saved SimpleCast session (store + disk)."""
    from core.playwright_session import has_session, clear_session
    sess_path = os.path.join(PROJECT_ROOT, "simplecast_session.json")
    if not has_session(sess_path):
        flash("No SimpleCast session found.", "warning")
        return redirect(url_for("settings.settings"))
    try:
        clear_session(sess_path)
        flash("SimpleCast session cleared.", "success")
    except OSError as e:
        flash(f"Could not clear SimpleCast session ({e}). Close any open Chrome windows and try again.", "danger")
    return redirect(url_for("settings.settings"))


@bp.route("/settings/clear-vista-social-session", methods=["POST"])
def clear_vista_social_session():
    """Clear the saved Vista Social session (store + disk)."""
    from core.playwright_session import has_session, clear_session
    sess_path = os.path.join(PROJECT_ROOT, "vista_social_session.json")
    if not has_session(sess_path):
        flash("No Vista Social session found.", "warning")
        return redirect(url_for("settings.settings"))
    try:
        clear_session(sess_path)
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
    """Clear the saved Rock session (store + disk)."""
    from core.playwright_session import has_session, clear_session
    sess_path = os.path.join(PROJECT_ROOT, "rock_session.json")
    if not has_session(sess_path):
        flash("No Rock session found.", "warning")
        return redirect(url_for("settings.settings"))
    try:
        clear_session(sess_path)
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
        flash("YouTube authentication successful!", "success")
    except FileNotFoundError as e:
        flash(str(e), "danger")
    except Exception as e:
        flash(f"YouTube authentication failed: {str(e)}", "danger")
    return redirect(url_for("scan.index"))


@bp.route("/oauth/youtube/settings", methods=["POST"])
def oauth_youtube_settings():
    """Trigger YouTube OAuth2 flow and redirect back to settings."""
    if is_hosted():
        return _hosted_youtube_oauth_redirect()
    try:
        get_authenticated_service()
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
        flash("YouTube authentication successful!", "success")
    except Exception as e:
        flash(f"YouTube authentication failed: {e}", "danger")
    return redirect(url_for("settings.settings"))


@bp.route("/settings/clear-youtube-token", methods=["POST"])
def clear_youtube_token():
    """Clear the stored YouTube OAuth token and redirect back to settings."""
    from uploaders.youtube_uploader import _clear_token, _YT_TOKEN_NAME
    from core import secrets_store
    if secrets_store.has_secret(_YT_TOKEN_NAME):
        _clear_token()
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
        secrets_store.set_secret(name, value)
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
        secrets_store.delete_secret(name)
        flash(f"Secret '{name}' cleared.", "success")
    except Exception as e:
        flash(f"Could not clear secret: {e}", "danger")
    return redirect(url_for("settings.settings"))


@bp.route("/settings/change-password", methods=["POST"])
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
