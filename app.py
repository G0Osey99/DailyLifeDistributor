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

from flask import Flask, abort, redirect, request, url_for


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
# the underlying call hits the encrypted store each time. The token now lives
# in the DB (no file mtime to watch), so we re-check on a simple TTL instead.
_YT_AUTH_CACHE: dict = {"value": None, "checked_at": 0.0}
_YT_AUTH_TTL_SEC = 30.0  # Navbar hint only: a cleared/added token can take up to this long to reflect.


def _cached_yt_authenticated() -> bool:
    """Cache the YouTube-authenticated state for the navbar.

    The token now lives in the encrypted store (no file mtime to watch), so
    we re-check at most every _YT_AUTH_TTL_SEC seconds rather than per request.
    """
    now = time.monotonic()
    if (now - _YT_AUTH_CACHE["checked_at"]) < _YT_AUTH_TTL_SEC and _YT_AUTH_CACHE["value"] is not None:
        return _YT_AUTH_CACHE["value"]
    try:
        val = bool(yt_is_authenticated())
    except Exception:
        logging.getLogger(__name__).debug(
            "_cached_yt_authenticated failed; treating as unauthenticated", exc_info=True
        )
        val = False
    _YT_AUTH_CACHE["value"] = val
    _YT_AUTH_CACHE["checked_at"] = now
    return val


def invalidate_yt_auth_cache() -> None:
    """Drop the cached YouTube-auth state so the next read re-checks the store.

    Called right after the token changes (OAuth success, Clear Token) so the
    Settings badge flips immediately instead of lagging up to the TTL.
    """
    _YT_AUTH_CACHE["value"] = None
    _YT_AUTH_CACHE["checked_at"] = 0.0


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

    # Secrets at rest require a valid master key — fail closed at startup with
    # a clear message rather than erroring deep inside an upload later.
    from core import crypto
    crypto.validate_master_key()

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=(
            os.environ.get("SESSION_COOKIE_SECURE", "true").lower()
            in ("1", "true", "yes")
        ),
        # Bound request bodies so a single multipart can't spool unbounded
        # bytes to disk. The chunk endpoint caps each chunk at 95 MB; allow
        # headroom for multipart overhead. Overridable for unusual setups.
        MAX_CONTENT_LENGTH=int(
            os.environ.get("MAX_CONTENT_LENGTH_BYTES", str(110 * 1024 * 1024))
        ),
    )

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

    from core import auth as _auth
    _auth.bootstrap_from_env()

    try:
        from scripts.migrate_secrets import run as _migrate_secrets
        _migrate_secrets()
    except Exception:
        logging.getLogger(__name__).exception(
            "Secret auto-import failed; continuing (run python -m scripts.migrate_secrets manually)."
        )

    # Restore browser sessions from the encrypted store. A container rebuild
    # wipes the materialized *_session.json files under /app, so without this
    # the box reads as logged-out after every redeploy even though the blobs
    # persist on the dld-data volume.
    try:
        from core.playwright_session import materialize_known_sessions
        _restored = materialize_known_sessions()
        if _restored:
            logging.getLogger(__name__).info(
                "Restored %d browser session(s) from the encrypted store.", _restored
            )
    except Exception:
        logging.getLogger(__name__).exception("Session restore at startup failed")

    from blueprints.auth import bp as auth_bp, is_authenticated
    app.register_blueprint(auth_bp)

    # Endpoints reachable without a session: the login routes, the health
    # probe, and static assets. Everything else requires authentication.
    _PUBLIC_ENDPOINTS = {"auth.login", "auth.login_submit", "_health", "static"}

    _ALLOWED_HOSTS = {
        h.strip().lower()
        for h in os.environ.get("ALLOWED_HOSTS", "").split(",")
        if h.strip()
    }

    @app.before_request
    def _require_auth():
        # DNS-rebind / host-spoofing defense for the hosted context: when
        # ALLOWED_HOSTS is configured, the Host header must match one of them.
        # Unset (local dev) = no host restriction.
        if _ALLOWED_HOSTS:
            host_no_port = (request.host or "").lower().split(":", 1)[0]
            if host_no_port not in _ALLOWED_HOSTS:
                abort(403)

        # Unmatched route: let Flask's 404 handler respond rather than
        # redirecting an unauthenticated probe to /login (info leak).
        if request.endpoint is None:
            return

        if request.endpoint in _PUBLIC_ENDPOINTS:
            return
        if is_authenticated():
            return
        wants_json = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in request.headers.get("Accept", "")
        )
        if wants_json:
            abort(401)
        return redirect(url_for("auth.login", next=request.path))

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

    _hsts_enabled = app.config["SESSION_COOKIE_SECURE"]

    @app.after_request
    def _security_headers(resp):
        """Defense-in-depth response headers.

        CSP confines script/connect/frame to same-origin: even if markup were
        injected, it can't load attacker scripts or exfiltrate to another
        origin, and the app can't be framed (clickjacking). 'unsafe-inline'
        is required because the templates use inline <script>/handlers; the
        same-origin connect/frame/form restrictions are the load-bearing part.
        The noVNC iframe + its WebSocket are same-origin, so 'self' covers them.
        """
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            # Google Fonts stylesheet + font files are the only third-party
            # origins the UI uses; everything else stays same-origin.
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "frame-src 'self'; "
            "frame-ancestors 'self'; "
            "base-uri 'self'; "
            # accounts.google.com is allowed so the hosted YouTube OAuth flow
            # can POST "Connect YouTube" and follow the redirect to Google's
            # consent screen; without it form-action 'self' blocks the hop.
            "form-action 'self' https://accounts.google.com; "
            "object-src 'none'",
        )
        # Only assert HSTS when we believe we're behind HTTPS, so a plain-http
        # local/dev run doesn't pin an unreachable https upgrade in the browser.
        if _hsts_enabled:
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return resp

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
    from blueprints.scan import bp as scan_bp
    from blueprints.settings import bp as settings_bp
    # upload_bp now exposes only the shared /upload/stream SSE endpoint that the
    # media pipeline consumes; the legacy /upload + /confirm + /review flow was
    # removed when the browser-streaming dashboard replaced it.
    from blueprints.upload import bp as upload_bp
    from blueprints.remote_login import bp as remote_login_bp
    from blueprints.media import bp as media_bp

    app.register_blueprint(scan_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(calendar_bp)
    app.register_blueprint(history_bp)
    app.register_blueprint(remote_login_bp)
    app.register_blueprint(media_bp)

    # Multi-tenant phase α: program-owner admin pages.
    from blueprints.admin import bp as admin_bp
    app.register_blueprint(admin_bp)

    # --- Rate limiter (Phase 3 hardening) -----------------------------------
    # Configured here on the app object so the agent blueprint can import the
    # shared instance. In-memory storage is fine for the single-instance VPS
    # deploy; switching to multi-instance is a one-line config change:
    #   RATELIMIT_STORAGE_URI=redis://... (the limiter picks it up via env).
    #
    # Tests disable rate limiting via app.config["RATELIMIT_ENABLED"]=False
    # (see tests/conftest.py — flipped on whenever TESTING=True), so the rest
    # of the suite can hit /agent/* and /pair/* repeatedly without 429s.
    # Production sets RATELIMIT_ENABLED=False only if the operator opts out.
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    _ratelimit_enabled = (
        os.environ.get("RATELIMIT_ENABLED", "true").lower() in ("1", "true", "yes")
    )
    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=[],  # no global default — opt-in per route
        storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
        enabled=_ratelimit_enabled,
        # Suppress flask-limiter's noisy warning when storage_uri=memory:// in
        # production-style deploys; that's the documented default for single
        # instance and the message scares operators reading the logs.
        in_memory_fallback_enabled=True,
    )
    # Stash on app.extensions so blueprints can grab the same instance without
    # re-importing the limiter (avoids two separate Limiter() instances).
    app.extensions["dld_limiter"] = limiter

    if os.environ.get("HYBRID_AGENT_ENABLED", "").lower() in ("1", "true", "yes"):
        from flask_sock import Sock
        from blueprints.agent import bp as agent_bp, register_sockets, attach_limits
        app.register_blueprint(agent_bp)
        # Apply rate limits on the now-registered routes (pair_new, pair_redeem).
        attach_limits(app, limiter)
        sock = Sock(app)
        register_sockets(sock)
        # Public (no-session) agent endpoints: the agent redeems a pairing code
        # and connects its token-authed socket before it has any session. Gated
        # with the feature so the exemptions only exist when the feature does.
        # _require_auth closes over this set, so mutating it here is seen there.
        _PUBLIC_ENDPOINTS.update({
            "agent.pair_redeem", "agent_socket",
            "agent.release_manifest", "agent.release_binary",
        })

    # Startup orphan sweep: clear any media-upload temp dirs left behind by a
    # previous process (crash / restart). No run is active yet, so pass empty.
    try:
        from core import media_session as _media_session
        removed = _media_session.sweep_orphans(active_run_ids=set())
        if removed:
            app.logger.info("media: swept %d orphaned upload temp dir(s)", removed)
    except Exception:  # noqa: BLE001
        app.logger.warning("media: startup orphan sweep failed", exc_info=True)

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
