"""Settings page, OAuth, llamafile/whisper status, Excel mapping, env-file editing."""
from __future__ import annotations

import json
import logging
import os
import threading

import requests
import yaml
from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from core.config import (
    CONFIG_PATH,
    ENV_PATH,
    PROJECT_ROOT,
    invalidate_config_cache,
    is_path_allowed,
    load_config,
)
from core.excel_parser import ExcelParser
from core.session_state import session
from core.quota import DAILY_QUOTA, get_quota_used
from uploaders.youtube_uploader import (
    get_authenticated_service,
    is_authenticated as yt_is_authenticated,
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
        try:
            current_app.logger.warning("Failed to read .env at %s: %s", ENV_PATH, e)
        except Exception:
            pass
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

        config.setdefault("whisper", {})
        config["whisper"]["model"] = request.form.get("whisper_model", "base")

        config.setdefault("description_footers", {})
        config["description_footers"]["youtube_video"] = request.form.get("footer_youtube_video", "")
        config["description_footers"]["youtube_shorts"] = request.form.get("footer_youtube_shorts", "")
        config["description_footers"]["podcast"] = request.form.get("footer_podcast", "")
        config["description_footers"]["vista_social"] = request.form.get("footer_vista_social", "")

        config.setdefault("directories", {})
        config["directories"]["base"] = request.form.get("dir_base", "")
        config["directories"]["youtube_video"] = request.form.get("dir_youtube_video", "")
        config["directories"]["youtube_shorts"] = request.form.get("dir_youtube_shorts", "")
        config["directories"]["podcast"] = request.form.get("dir_podcast", "")
        config["directories"]["thumbnails"] = request.form.get("dir_thumbnails", "")
        # Only overwrite the email-thumbnail dir when the field is present, so
        # a Settings page that predates this field can't blank an existing value.
        if "dir_email_thumbnails" in request.form:
            config["directories"]["email_thumbnails"] = request.form.get("dir_email_thumbnails", "")
        config["sharepoint_docx"] = request.form.get("sharepoint_docx", "")

        # M12: a full or read-only USB used to 500 the Settings POST. Catch
        # write failures and surface them as flash messages instead.
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
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

        flash("Settings saved successfully!", "success")
        return redirect(url_for("settings.settings"))

    config = load_config()

    secrets_path = os.path.join(PROJECT_ROOT, "client_secrets.json")
    client_secrets_found = os.path.isfile(secrets_path)
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
    secret_names = [n for n in secrets_store.list_secret_names()
                    if n != _HASH_SECRET]
    return render_template(
        "settings.html",
        config=config,
        client_secrets_found=client_secrets_found,
        simplecast_session_found=simplecast_session_found,
        vista_social_session_found=vista_social_session_found,
        rock_session_found=rock_session_found,
        youtube_authenticated=_cached_yt_authenticated(),
        secret_names=secret_names,
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

    Used by the per-service Login buttons in Settings: opening the
    PlaywrightSession context manager runs `_handle_login` automatically when
    no session file is present (or when the saved session redirects to a
    login page), and the storage_state is persisted on context exit.
    """
    from core.playwright_session import PlaywrightSession
    cfg = build_config()
    with PlaywrightSession(cfg):
        pass


@bp.route("/settings/login-simplecast", methods=["POST"])
def login_simplecast():
    """Open Chrome and walk the user through SimpleCast login."""
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


@bp.route("/oauth/youtube", methods=["POST"])
def oauth_youtube():
    """Trigger YouTube OAuth2 flow."""
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
    try:
        get_authenticated_service()
        flash("YouTube authentication successful!", "success")
    except FileNotFoundError as e:
        flash(str(e), "danger")
    except Exception as e:
        flash(f"YouTube authentication failed: {str(e)}", "danger")
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


# Whisper download status: model_name -> {"status": "running"|"done"|"error", "error": str}
_whisper_download_status: dict[str, dict] = {}
_whisper_download_lock = threading.Lock()


def _download_whisper_model_worker(model_name: str, cache_dir: str | None) -> None:
    try:
        from faster_whisper import WhisperModel
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        WhisperModel(model_name, device="cpu", compute_type="int8", download_root=cache_dir)
        with _whisper_download_lock:
            _whisper_download_status[model_name] = {"status": "done", "error": ""}
    except Exception as e:
        with _whisper_download_lock:
            _whisper_download_status[model_name] = {"status": "error", "error": str(e)}


@bp.route("/settings/download-whisper-model")
def download_whisper_model():
    """Kick off Whisper model download in the background.

    The medium/large models can take many minutes to fetch; running that
    inside the request thread caused the browser to time out. Now we
    return immediately and the user can poll /settings/whisper-status.
    """
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        flash("faster-whisper is not installed. Run: pip install faster-whisper", "danger")
        return redirect(url_for("settings.settings"))

    config = load_config()
    model_name = config.get("whisper", {}).get("model", "base")
    cache_dir = os.environ.get("WHISPER_DOWNLOAD_ROOT", None)

    with _whisper_download_lock:
        current = _whisper_download_status.get(model_name)
        if current and current.get("status") == "running":
            flash(f"Whisper model '{model_name}' is already downloading…", "info")
            return redirect(url_for("settings.settings"))
        _whisper_download_status[model_name] = {"status": "running", "error": ""}

    threading.Thread(
        target=_download_whisper_model_worker,
        args=(model_name, cache_dir),
        daemon=True,
    ).start()
    flash(
        f"Whisper model '{model_name}' download started in the background. "
        "Check the Settings page in a few minutes.",
        "info",
    )
    return redirect(url_for("settings.settings"))


@bp.route("/settings/whisper-status")
def whisper_download_status():
    """Return current download status for the configured Whisper model."""
    model_name = load_config().get("whisper", {}).get("model", "base")
    with _whisper_download_lock:
        return jsonify({"model": model_name, **(_whisper_download_status.get(model_name) or {"status": "idle", "error": ""})})


@bp.route("/llamafile/status")
def llamafile_status():
    """Return llamafile server status."""
    try:
        requests.get("http://localhost:8081/v1/models", timeout=5)
        running = True
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout):
        running = False
    except Exception as e:
        # M14: previous code reported running=True on any non-network
        # exception, which masked real bugs and lied to the user. Fail
        # closed and log so the Settings page reflects reality.
        current_app.logger.warning("llamafile status check raised: %s", e)
        running = False
    return jsonify({
        "running": running,
        "model": "llama3.2",
        "port": 8081
    })


@bp.route("/settings/excel-sheets")
def excel_sheets():
    """Return sheet names for the given Excel file."""
    from core.excel_parser import get_sheet_names
    path = request.args.get("path", "")
    if not path or not os.path.isfile(path):
        return jsonify({"sheets": [], "error": "File not found"}), 404
    if not is_path_allowed(path):
        return jsonify({"sheets": [], "error": "Path is outside the allowed roots."}), 403
    sheets = get_sheet_names(path)
    return jsonify({"sheets": sheets})


@bp.route("/settings/excel-columns")
def excel_columns():
    """Return column names for a sheet in an Excel file."""
    from core.excel_parser import get_column_names
    path = request.args.get("path", "")
    sheet = request.args.get("sheet", "")
    if not path or not sheet:
        return jsonify({"columns": []}), 400
    if not is_path_allowed(path):
        return jsonify({"columns": [], "error": "Path is outside the allowed roots."}), 403
    columns = get_column_names(path, sheet)
    return jsonify({"columns": columns})


@bp.route("/settings/excel-preview")
def excel_preview():
    """Return first 5 rows of a sheet for preview."""
    from core.excel_parser import get_sheet_preview
    path = request.args.get("path", "")
    sheet = request.args.get("sheet", "")
    if not path or not sheet:
        return jsonify({"rows": []}), 400
    if not is_path_allowed(path):
        return jsonify({"rows": [], "error": "Path is outside the allowed roots."}), 403
    rows = get_sheet_preview(path, sheet)
    return jsonify({"rows": rows})


@bp.route("/settings/save-excel-mapping", methods=["POST"])
def save_excel_mapping():
    """Save Excel column mapping to config.yaml."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    config = load_config()
    config["excel_mapping"] = {
        "sheet_name": data.get("sheet_name", ""),
        "date_column": data.get("date_column", ""),
        "shorts_title_column": data.get("shorts_title_column", ""),
        "description_column": data.get("description_column", ""),
        "vista_caption_column": data.get("vista_caption_column", ""),
        "tags_column": data.get("tags_column", ""),
        "youtube_title_column": data.get("youtube_title_column", ""),
        "podcast_title_column": data.get("podcast_title_column", ""),
        "passage_column": data.get("passage_column", ""),
        "scripture_column": data.get("scripture_column", ""),
        "episode_title_column": data.get("episode_title_column", ""),
        "prayer_column": data.get("prayer_column", ""),
        "topic_column": data.get("topic_column", ""),
    }

    # M12: surface a JSON error rather than 500 on disk-full / read-only USB.
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except OSError as e:
        return jsonify({"success": False, "error": f"Could not save config.yaml: {e}"}), 500
    invalidate_config_cache()
    ExcelParser(load_config()).invalidate_cache()
    session.reload_config()

    return jsonify({"success": True})


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
