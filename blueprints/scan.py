"""Index page + directory browsing/scan routes."""
from __future__ import annotations

import os
from pathlib import Path

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

from core import db as _db
from core.config import is_path_allowed, load_config, resolved_dirs
from core.file_scanner import FileScanner
from core.session_state import session
from uploaders.youtube_uploader import is_authenticated as yt_is_authenticated

bp = Blueprint("scan", __name__)


@bp.route("/", methods=["GET", "POST"])
def index():
    config = load_config()

    if request.method == "POST":
        selected_dates = request.form.getlist("dates")
        if not selected_dates:
            flash("Please select at least one date.", "warning")
            return redirect(url_for("scan.index"))

        # All platforms default ON; per-date toggles on the Review page handle
        # disabling. The Daily Life email channel is opt-in — it follows its
        # config.yaml `platforms.rock_email` flag rather than defaulting on,
        # because it depends on each date's YouTube Video upload (or a
        # provided link) being present in the run.
        global_platforms = {
            "youtube_video": True,
            "youtube_shorts": True,
            "simplecast": True,
            "rock": True,
            "rock_email": config.get("platforms", {}).get("rock_email", False),
            "vista_social": True,
        }

        sched = config.get("scheduling", {})
        global_times = {
            "youtube_video": request.form.get("global_time_youtube_video") or sched.get("youtube_video", "10:00"),
            "youtube_shorts": request.form.get("global_time_youtube_shorts") or sched.get("youtube_shorts", "12:00"),
            "simplecast": request.form.get("global_time_simplecast") or sched.get("simplecast", "06:00"),
            "vista_social": request.form.get("global_time_vista_social") or sched.get("vista_social", "12:00"),
        }
        session.global_times = global_times

        current_app.logger.debug("Path overrides at review load time: %s", session.path_overrides)
        session.load_for_dates(selected_dates, global_platforms, path_overrides=session.path_overrides)
        return redirect(url_for("review.review"))

    scanner = FileScanner(config)
    available_dates = scanner.get_available_dates()
    platforms = config.get("platforms", {})
    scheduling = config.get("scheduling", {})

    resume_session_row = _db.get_latest_in_progress()

    return render_template(
        "index.html",
        dates=available_dates,
        platforms=platforms,
        scheduling=scheduling,
        youtube_authenticated=yt_is_authenticated(),
        resume_session=resume_session_row,
    )


@bp.route("/scan-config")
def scan_config():
    """Return current resolved directory paths (merged config + session overrides)."""
    config = load_config()
    config_dirs = resolved_dirs(config)
    result = {}
    for key, config_path in config_dirs.items():
        override = session.path_overrides.get(key)
        if override:
            result[key] = {"path": override, "source": "session"}
        else:
            result[key] = {"path": config_path, "source": "config"}
    docx_override = session.path_overrides.get("sharepoint_docx")
    if docx_override:
        result["sharepoint_docx"] = {"path": docx_override, "source": "session"}
    else:
        result["sharepoint_docx"] = {"path": config.get("sharepoint_docx", ""), "source": "config"}
    return jsonify(result)


@bp.route("/validate-path", methods=["POST"])
def validate_path():
    """Validate a directory or file path."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400
    path = data.get("path", "")
    path_type = data.get("type", "")
    if not isinstance(path, str) or len(path) > _MAX_PATH_LEN or "\x00" in path:
        return jsonify({"error": "Invalid path"}), 400
    if not is_path_allowed(path):
        return jsonify({"error": "Path is outside the allowed roots (your home folder or the configured media drive)."}), 403
    result = FileScanner.validate_path(path)
    if path_type == "sharepoint_docx":
        result["is_xlsx"] = path.lower().endswith(".xlsx")
    return jsonify(result)


@bp.route("/scan", methods=["POST"])
def scan():
    """Scan directories with optional path overrides. Returns list of MediaDateEntry dicts."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    paths = data.get("paths", {})
    selected_dates = data.get("selected_dates")

    if not isinstance(paths, dict):
        return jsonify({"error": "Invalid paths object"}), 400

    # Match the validation /validate-path and /browse already enforce: cap
    # length and reject NUL bytes so a stray binary value can't reach the
    # filesystem layer. Loopback gating is the only other defense here.
    for k, v in list(paths.items()):
        if v is None or v == "":
            continue
        if not isinstance(v, str) or len(v) > _MAX_PATH_LEN or "\x00" in v:
            return jsonify({"error": f"Invalid path for {k}"}), 400
        if not is_path_allowed(v):
            return jsonify({"error": f"Path for {k} is outside the allowed roots."}), 403

    # Only persist overrides for paths that actually exist on disk —
    # otherwise a typo sticks in session.path_overrides until the user
    # notices and overwrites it. The xlsx path is a file; the rest are
    # directories.
    for key in ("youtube_video", "youtube_shorts", "podcast", "thumbnails", "sharepoint_docx"):
        candidate = paths.get(key)
        if not candidate:
            continue
        if key == "sharepoint_docx":
            valid = os.path.isfile(candidate)
        else:
            valid = os.path.isdir(candidate)
        if valid:
            session.path_overrides[key] = candidate
        else:
            current_app.logger.warning(
                "Ignoring invalid %s path override: %s", key, candidate
            )

    config = load_config()
    current_app.logger.debug("Path overrides at scan time: %s", paths)
    scanner = FileScanner(config)
    entries = scanner.scan_custom_paths(paths)

    if selected_dates:
        date_set = set(selected_dates)
        entries = [e for e in entries if e.date in date_set]

    return jsonify({
        "dates": [
            {
                "date": e.date,
                "display_date": e.display_date,
                "youtube_video_path": e.youtube_video_path,
                "youtube_shorts_path": e.youtube_shorts_path,
                "podcast_path": e.podcast_path,
                "thumbnail_path": e.thumbnail_path,
            }
            for e in entries
        ]
    })


_MAX_PATH_LEN = 4096  # well above any realistic OS path; pure DoS guard


@bp.route("/browse")
def browse():
    """Return a JSON directory listing for a given path."""
    path = request.args.get("path", "")
    # Guard against absurd inputs; the route already requires loopback so
    # this is just a sanity cap rather than a security boundary.
    if len(path) > _MAX_PATH_LEN:
        return jsonify({"error": "Path too long"}), 400
    # Reject NUL bytes — os.listdir on a NUL-containing path raises
    # ValueError on POSIX and TypeError on Windows; turn it into a 400.
    if "\x00" in path:
        return jsonify({"error": "Invalid path"}), 400

    if path:
        candidate = Path(path)
        while candidate != candidate.parent:
            if candidate.exists() and candidate.is_dir():
                break
            candidate = candidate.parent
        else:
            candidate = Path.home()
        if not candidate.exists():
            candidate = Path.home()
        path = str(candidate)
    else:
        config = load_config()
        base = config.get("directories", {}).get("base", "")
        path = base if base and os.path.isdir(base) else str(Path.home())

    # Confine browsing to the user's home, the project root, and the
    # configured media base. If the requested (or walked-up-to) path falls
    # outside, snap back to home rather than expose unrelated parts of the
    # filesystem.
    if not is_path_allowed(path):
        path = str(Path.home())

    result = {
        "current": path,
        "parent": str(Path(path).parent) if path else None,
        "dirs": [],
        "files": [],
        "error": None,
    }

    if not os.path.exists(path):
        result["error"] = "Path does not exist"
        return jsonify(result)

    if not os.path.isdir(path):
        result["error"] = "Path is not a directory"
        return jsonify(result)

    try:
        entries = os.listdir(path)
        for name in sorted(entries):
            full = os.path.join(path, name)
            try:
                if os.path.isdir(full):
                    result["dirs"].append(name)
                else:
                    result["files"].append(name)
            except PermissionError:
                continue
    except PermissionError:
        result["error"] = "Permission denied"
    except OSError as e:
        result["error"] = str(e)
    except UnicodeDecodeError as e:
        # M10: mixed-encoding filesystems (rare on macOS/Linux network mounts)
        # would otherwise crash the listing with no actionable error.
        result["error"] = f"Filename encoding error: {e}"

    return jsonify(result)
