"""Flask entry point for DailyLifeDistributor media uploader.

Slim factory module. Routes live in `blueprints/`, shared helpers in `core/`.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import time

from dotenv import load_dotenv

from core.config import ENV_PATH, PROJECT_ROOT

load_dotenv(ENV_PATH)

from flask import Flask, abort, request


def _configure_file_logging() -> None:
    """Attach a rotating file handler so unattended uploads leave a log trail.

    Logs go to `logs/daily_life.log` at the project root, rotating at 5 MB
    with 5 backups. The console handler stays in place — this is purely
    additive. Idempotent: the dedupe check keeps duplicate handlers off the
    root logger when create_app() is called twice (e.g. tests).
    """
    log_dir = os.path.join(PROJECT_ROOT, "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as e:
        # L1: print once to stderr so a missing file log isn't completely
        # invisible. Console logging still works regardless.
        import sys
        print(f"[warn] could not create log dir {log_dir}: {e} (console-only)", file=sys.stderr)
        return

    log_path = os.path.join(log_dir, "daily_life.log")
    root = logging.getLogger()
    if any(getattr(h, "_dld_file_handler", False) for h in root.handlers):
        return

    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s"
    ))
    handler._dld_file_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)

from core import db as _db
from core.env_validation import validate_env
from uploaders.youtube_uploader import is_authenticated as yt_is_authenticated


# Cache yt_is_authenticated() — context processor runs on every render and
# the underlying call hits disk + parses JSON each time. Keyed off token.json
# mtime so a cleared/refreshed token invalidates immediately.
_YT_AUTH_CACHE: dict = {"mtime": None, "value": False, "checked_at": 0.0}
_YT_AUTH_TTL_SEC = 30.0


def _cached_yt_authenticated() -> bool:
    from uploaders.youtube_uploader import _get_token_path
    try:
        token_path = _get_token_path()
        mtime = os.path.getmtime(token_path) if os.path.exists(token_path) else None
        now = time.time()
        if (
            _YT_AUTH_CACHE["mtime"] == mtime
            and now - _YT_AUTH_CACHE["checked_at"] < _YT_AUTH_TTL_SEC
        ):
            return _YT_AUTH_CACHE["value"]
        value = bool(yt_is_authenticated())
        _YT_AUTH_CACHE.update(mtime=mtime, value=value, checked_at=now)
        return value
    except Exception:
        logging.getLogger(__name__).debug(
            "_cached_yt_authenticated failed; treating as unauthenticated", exc_info=True
        )
        return False


def create_app() -> Flask:
    _configure_file_logging()
    validate_env()
    app = Flask(__name__)

    secret = os.environ.get("FLASK_SECRET_KEY")
    if not secret:
        # The session cookie signs flash messages and quota counters — a
        # missing key means anyone who knows the default could forge a
        # signed session. Fail loudly rather than silently downgrade.
        logging.getLogger(__name__).warning(
            "FLASK_SECRET_KEY is not set; using an ephemeral random key. "
            "Sessions will not survive a restart."
        )
        secret = os.urandom(32).hex()
    app.secret_key = secret

    # M25: a corrupt state.db (USB unplug mid-write, stray SIGKILL) used to
    # crash app boot with an opaque sqlite3 traceback. Surface a clear,
    # actionable message instead — the user can move/delete state.db and
    # we'll create a fresh one on the next launch.
    try:
        _db.init_db()
        # Backfill once per process (idempotent but scans the whole table).
        # The first run after a schema bump still does the work; subsequent
        # restarts within the same process lifetime skip it.
        _db.backfill_external_ids()
    except Exception as db_exc:
        logging.getLogger(__name__).exception(
            "Failed to initialize state.db at %s. The file may be corrupt — "
            "back it up and delete it to recover. Error: %s",
            _db._DB_PATH, db_exc,
        )
        raise

    @app.before_request
    def _restrict_to_loopback():
        # The app has no auth and exposes /browse, /validate-path, /scan,
        # which can read arbitrary directories. Reject anything not coming
        # from loopback so a stray bind to 0.0.0.0 (or a misconfigured proxy)
        # can't accidentally expose the filesystem.
        remote = (request.remote_addr or "").strip()
        if remote and remote not in ("127.0.0.1", "::1"):
            abort(403)

        # Defeat DNS rebinding: a malicious website can rebind its hostname
        # to 127.0.0.1 so the browser sends same-origin requests to us. The
        # remote_addr check above passes (it really is 127.0.0.1), but the
        # Host header still carries the attacker's domain. Only accept Host
        # headers that name loopback explicitly.
        host = (request.host or "").lower()
        host_no_port = host.split(":", 1)[0]
        allowed_hosts = {"localhost", "127.0.0.1", "[::1]", "::1"}
        if host_no_port not in allowed_hosts:
            abort(403)

    @app.before_request
    def _csrf_same_origin():
        """Reject cross-origin state-changing requests.

        Loopback restriction alone doesn't stop CSRF: any browser tab on
        the same machine — including malicious local pages and other
        localhost services — can POST to us via fetch/form submission.
        Modern browsers send Sec-Fetch-Site on every navigation and
        Origin on every cross-origin POST, so we can require
        same-origin without rolling out CSRF tokens to every template.

        GETs are intentionally exempt: they must be safe (no state
        change), and several of them are linked from the OAuth callback
        which arrives as a navigation with Sec-Fetch-Site=cross-site.
        """
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        sfs = request.headers.get("Sec-Fetch-Site", "")
        if sfs in ("same-origin", "same-site", "none"):
            return
        # Older browsers / curl won't send Sec-Fetch-Site; fall back to Origin.
        origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
        if not origin:
            # No header at all — treat as a non-browser client (curl, tests).
            # Loopback restriction above already blocks remote callers, so
            # this is safe for the local CLI/test workflow.
            return
        host_url = request.host_url.rstrip("/")
        if origin.startswith(host_url):
            return
        abort(403)

    @app.context_processor
    def inject_global_context():
        """Make youtube_authenticated available in all templates (used by navbar)."""
        return {"youtube_authenticated": _cached_yt_authenticated()}

    @app.route("/health")
    def _health():
        """Lightweight readiness probe for on-call diagnosis.

        Reports the state of the three runtime dependencies a freshly-launched
        instance can lose silently: the SQLite file, the bundled llamafile
        server (port 8081), and a Chrome binary that Playwright can find.

        Returns 200 when every check passes and 503 otherwise so a curl on
        port 8080 gives an actionable verdict at 3am without rendering a
        template (templates depend on the very things this check covers).
        """
        from flask import jsonify
        checks: dict = {}

        try:
            with _db._get_conn() as conn:
                conn.execute("SELECT 1").fetchone()
            checks["db"] = {"ok": True, "path": _db._DB_PATH}
        except Exception as e:
            checks["db"] = {"ok": False, "path": _db._DB_PATH, "error": str(e)}

        try:
            from core.llm_title_gen import is_llamafile_running, LLAMAFILE_BASE_URL
            checks["llamafile"] = {"ok": bool(is_llamafile_running()), "url": LLAMAFILE_BASE_URL}
        except Exception as e:
            checks["llamafile"] = {"ok": False, "error": str(e)}

        chrome_path = (
            os.environ.get("SIMPLECAST_CHROME_PATH")
            or os.environ.get("VISTA_SOCIAL_CHROME_PATH")
            or os.environ.get("ROCK_CHROME_PATH")
            or ""
        ).strip()
        if chrome_path:
            checks["chrome"] = {"ok": os.path.isfile(chrome_path), "path": chrome_path}
        else:
            # Playwright will resolve `channel='chrome'` against the system
            # Chrome at runtime — we can't probe that without launching, so
            # report the env-var configuration honestly instead of guessing.
            checks["chrome"] = {"ok": True, "path": "channel=chrome (system default)"}

        ok = all(c.get("ok") for c in checks.values())
        return jsonify({"ok": ok, "checks": checks}), (200 if ok else 503)

    from blueprints.calendar import bp as calendar_bp
    from blueprints.history import bp as history_bp
    from blueprints.review import bp as review_bp
    from blueprints.scan import bp as scan_bp
    from blueprints.settings import bp as settings_bp
    from blueprints.upload import bp as upload_bp

    app.register_blueprint(scan_bp)
    app.register_blueprint(review_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(calendar_bp)
    app.register_blueprint(history_bp)

    return app


# Module-level instance so `import app; app.app` and `flask --app app` both work.
app = create_app()


def _check_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Return True if `host:port` accepts a fresh bind right now.

    Why pre-flight: when the port is already in use, Flask either fails
    silently or hangs waiting on accept(), which on-call sees as "I clicked
    launch and nothing happened" with no log line to grep. A two-line
    socket probe surfaces it as a clear stderr message before app.run().
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


if __name__ == "__main__":
    import sys
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    try:
        port = int(os.environ.get("FLASK_PORT", "8080"))
    except ValueError:
        port = 8080

    if not _check_port_free(port):
        print(
            f"[fatal] port {port} is already in use on 127.0.0.1. "
            "Another DailyLifeDistributor instance, an old Flask process, "
            "or another local server is holding it. Stop that process or "
            "set FLASK_PORT to an unused port and re-launch.",
            file=sys.stderr,
        )
        sys.exit(2)

    # llamafile (port 8081) is a soft dependency — title suggestions just
    # disappear without it. Warn but do not abort: the rest of the app is
    # still useful for actual uploads.
    try:
        from core.llm_title_gen import is_llamafile_running
        if not is_llamafile_running():
            print(
                "[warn] llamafile (port 8081) is not responding. YouTube "
                "Shorts title suggestions will be unavailable until it's "
                "started. Re-run launch_mac.command or check bin/llamafile.",
                file=sys.stderr,
            )
    except Exception as _llama_exc:
        logging.getLogger(__name__).debug("llamafile pre-flight skipped: %s", _llama_exc)

    app.run(host="127.0.0.1", port=port, debug=debug)
