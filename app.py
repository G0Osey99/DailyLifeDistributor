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

    # Security: refuse to boot when the legacy shared-password is enabled on
    # the hosted multi-tenant deploy. Legacy sessions bypass @require_role,
    # @require_program_owner, and @require_authenticated_json (see
    # core/permissions.py); mixing that with multi-tenant invitations would
    # let any holder of the shared password act with every role in every
    # org. Tests / local USB installs can still flip the env var; the gate
    # only fires when HOSTED=true.
    from core.hosted import is_hosted
    _legacy_on = (os.environ.get("LEGACY_PASSWORD_ENABLED", "") or "").lower() in (
        "1", "true", "yes",
    )
    if _legacy_on and is_hosted():
        raise RuntimeError(
            "Refusing to boot: LEGACY_PASSWORD_ENABLED=true on a HOSTED "
            "deploy bypasses all role gates. Unset one of LEGACY_PASSWORD_ENABLED "
            "or HOSTED before restarting. Multi-tenant rollback path is to "
            "set LEGACY_PASSWORD_ENABLED=true ONLY on the single-tenant USB "
            "install (HOSTED unset)."
        )

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
        # Public-facing base URL used when generating absolute links for
        # outbound emails (invites, recovery, new-device). Until this was
        # wired, callers fell back to a hardcoded autoalert.pro string in
        # three modules — a custom deploy on a different host would have
        # mailed everyone links pointing at LCBC's instance.
        BASE_URL=os.environ.get("BASE_URL", "https://autoalert.pro").rstrip("/"),
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

    # Multi-tenant phase α: idempotent first-boot migration.
    try:
        from core.migration_bootstrap import run_migration as _run_mt_migration
        _run_mt_migration()
    except Exception:
        logging.getLogger(__name__).exception(
            "Multi-tenant migration_bootstrap.run_migration() failed. "
            "First boot requires PROGRAM_OWNER_EMAIL + INITIAL_ADMIN_PASSWORD."
        )
        # We deliberately do NOT re-raise: a missing env var on later boots
        # (after the bootstrap user already exists) must not block startup.
        # The check inside run_migration() handles the "already bootstrapped"
        # path explicitly — only a true first-boot misconfig logs + continues.

    try:
        from scripts.migrate_secrets import run as _migrate_secrets
        _migrate_secrets()
    except Exception:
        logging.getLogger(__name__).exception(
            "Secret auto-import failed; continuing (run python -m scripts.migrate_secrets manually)."
        )


    from blueprints.auth import bp as auth_bp, is_authenticated
    app.register_blueprint(auth_bp)

    # Endpoints reachable without a session: the login routes, the health
    # probe, static assets, and the invite-accept GET/POST (so a brand-new
    # invitee can hit the signup form without an existing session).
    _PUBLIC_ENDPOINTS = {
        "auth.login", "auth.login_submit", "_health", "_health_details", "_health_alerts", "static",
        "invitations.accept_get", "invitations.accept_post",
        # Phase γ: second-factor screens (the session isn't fully
        # authenticated until the second factor is verified, so a
        # login_required gate would loop the user back to /login).
        "auth.login_2fa_get", "auth.login_2fa_post",
        "auth.login_email_2fa_get", "auth.login_email_2fa_post",
        # First-login forced password change — partial-token-gated, no session yet.
        "auth.first_password_set_get", "auth.first_password_set_post",
        # Session-status feed. Public at the routing layer; the route
        # itself does an inline auth check that accepts either a
        # session cookie OR a valid agent pair token via ?token=.
        # Without that path the agent GUI can't poll it (no cookie),
        # and its Sessions panel stays stuck on "unknown".
        "settings.sessions_status",
    }

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

    # Phase γ: enforce org-level "Require 2FA" on protected paths.
    # We deliberately exempt LEGACY_PASSWORD_ENABLED sessions (no user_id)
    # so the existing test suite + ops rollback path don't trip this gate.
    _ENFORCE_2FA_PATHS = ("/", "/upload", "/media/", "/review", "/confirm")
    _EXEMPT_2FA_PATHS = (
        "/settings/2fa", "/settings/security", "/logout", "/static",
        "/login", "/recover",
    )

    @app.before_request
    def _enforce_2fa():
        # NOTE on error handling: every except-pass here previously dropped
        # DB / session errors without a trace. Replaced with log.exception:
        # the enforcer still falls through (user proceeds to the page they
        # requested — they ARE authenticated; we'd just skipped pushing
        # them to /settings/2fa). Ops now sees a stack instead of silence.
        _log = logging.getLogger(__name__)
        from flask import session as _sess
        try:
            uid = _sess.get("user_id")
        except Exception:
            _log.exception("2FA enforcer: session read failed")
            return
        if not uid:
            return
        org_id = _sess.get("current_org_id")
        if not org_id:
            return
        try:
            org = _db.get_org(org_id)
        except Exception:
            _log.exception("2FA enforcer: get_org(%s) failed", org_id)
            return
        if not org or not org.get("require_2fa"):
            return
        try:
            user = _db.get_user_by_id(uid)
        except Exception:
            _log.exception("2FA enforcer: get_user_by_id(%s) failed", uid)
            return
        if not user:
            return
        if user.get("totp_enabled") or user.get("email_2fa_enabled"):
            return
        p = request.path
        if any(p.startswith(e) for e in _EXEMPT_2FA_PATHS):
            return
        if any(p == g or p.startswith(g) for g in _ENFORCE_2FA_PATHS):
            return redirect(url_for("twofa.settings_2fa"))

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

    @app.context_processor
    def _inject_membership_context():
        # Multi-tenant phase α: header switch-org dropdown only renders when
        # the user has more than one membership.
        from core import auth as _auth, org_store as _os, user_store as _us
        uid = _auth.current_user_id()
        # `is_authenticated` covers both the new user_id session and the
        # legacy shared-password session, so the Sign-out button shows for
        # both forms.
        signed_in = _auth.is_authenticated()
        if uid is None:
            return {
                "current_memberships": [],
                "current_org_id": None,
                "current_username": None,
                "is_signed_in": signed_in,
            }
        try:
            mems = _os.list_memberships_for_user(uid)
        except Exception:
            mems = []
        try:
            user = _us.get_user_by_id(uid)
            username = (user or {}).get("username")
            is_program_owner = bool((user or {}).get("program_owner"))
        except Exception:
            username = None
            is_program_owner = False
        # Pick the current org's role for this user so the sidebar / settings
        # can conditionally render owner-only entries.
        current_role = None
        try:
            oid = _auth.current_org_id()
            if oid is not None:
                m = next((m for m in mems if m["org_id"] == oid), None)
                if m:
                    current_role = m.get("role")
        except Exception:
            current_role = None
        return {
            "current_memberships": mems,
            "current_org_id": _auth.current_org_id(),
            "current_username": username,
            "is_signed_in": signed_in,
            "is_program_owner": is_program_owner,
            "current_role": current_role,
        }

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

    @app.route("/health/details")
    def _health_details():
        """Operational telemetry endpoint — does NOT decide /health's 200/503.

        Surfaces signals that an external monitor (Uptime Kuma, Pingdom)
        should fire on but that aren't strictly "service down" — e.g. a
        tripped circuit breaker, an empty Resend key, YT quota near cap.

        Public endpoint. Returns 200 always; the caller interprets the
        payload. Adding new keys is backward-compatible; renaming an
        existing key is a breaking change for whoever scrapes it.
        """
        from flask import jsonify
        details: dict = {}

        # 1. Circuit breakers (relay outages, image-provider down, LLM down).
        try:
            from core.circuit_breaker import _registry  # type: ignore[attr-defined]
            details["breakers"] = {
                name: {
                    "state": b.state.value,
                    "consecutive_failures": b._consecutive_failures,  # type: ignore[attr-defined]
                }
                for name, b in _registry.items()  # type: ignore[attr-defined]
            }
        except Exception as e:
            details["breakers"] = {"error": str(e)}

        # 2. Relay agent count — paired & connected RIGHT NOW.
        try:
            from core.relay import online_agent_count
            details["agents_online"] = online_agent_count()
        except Exception:
            details["agents_online"] = 0

        # 3. Resend reachability — only "is the key present?" (no API ping,
        # we don't want /health/details to burn email credits or block
        # the response on Resend latency). pip-audit pattern: configured
        # = green; missing = warn; doesn't validate the key works.
        details["resend_configured"] = bool(
            (os.environ.get("RESEND_API_KEY") or "").strip()
        )

        # 4. YouTube quota — bytes used / cap, for alerting at ≥90%.
        try:
            from core.quota import get_quota_used, DAILY_QUOTA
            used = int(get_quota_used() or 0)
            details["youtube_quota"] = {
                "used": used,
                "cap": int(DAILY_QUOTA),
                "pct": round(100.0 * used / max(1, DAILY_QUOTA), 1),
            }
        except Exception as e:
            details["youtube_quota"] = {"error": str(e)}

        # 5. SECRET_ENC_KEY presence (caught at boot, but echo for monitors).
        details["secret_enc_key_set"] = bool(
            (os.environ.get("SECRET_ENC_KEY") or "").strip()
        )

        return jsonify(details), 200

    @app.route("/health/alerts")
    def _health_alerts():
        """Single endpoint an uptime monitor can watch on HTTP-status alone.

        Returns 200 + ``{"ok": True, "alerts": []}`` when nothing is
        actionable. Returns 503 + ``{"ok": False, "alerts": [{...}, ...]}``
        when something needs human attention. The response body lists the
        specific firing conditions so the alert email tells ops *what*
        broke, not just *that* something broke.

        What counts as 'actionable':
          * DB unreachable (SQLite write path is dead)
          * Any circuit breaker OPEN (Resend, image providers, LLM)
          * YouTube quota >= 95% (uploads will start failing soon)
          * SECRET_ENC_KEY missing (config drift mid-flight)

        What's reported but NOT 503 (use /health/details for these):
          * Resend not configured (may be intentional pre-rollout)
          * agents_online == 0 (no SLA on that)
          * YT quota 90-94% (warning, not critical)

        Public endpoint, no auth — meant to be polled by external services.
        """
        from flask import jsonify
        alerts: list[dict] = []

        # DB write probe — if state.db has rotated out from under us
        # (USB unplug, volume detached), every upload + auth call dies.
        try:
            with _db._get_conn() as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception as e:
            alerts.append({
                "severity": "critical",
                "code": "db_unreachable",
                "message": f"SQLite at {_db._DB_PATH} not readable: {e}",
            })

        # Any open breaker = a real integration is failing fast for a
        # cooldown window. Pages that depend on it will mostly succeed
        # by serving cached/degraded results — but operators should
        # know the underlying provider is sick.
        try:
            from core.circuit_breaker import _registry  # type: ignore[attr-defined]
            for name, b in _registry.items():  # type: ignore[attr-defined]
                if b.state.value == "open":
                    alerts.append({
                        "severity": "warning",
                        "code": f"breaker_open:{name}",
                        "message": (
                            f"Circuit breaker '{name}' is OPEN "
                            f"(consecutive_failures={b._consecutive_failures}). "  # type: ignore[attr-defined]
                            "Downstream integration is failing fast for the cooldown window."
                        ),
                    })
        except Exception as e:
            alerts.append({
                "severity": "warning",
                "code": "breaker_introspection_failed",
                "message": str(e),
            })

        # YT quota at 95%+ — uploads will start refusing within minutes.
        try:
            from core.quota import get_quota_used, DAILY_QUOTA
            used = int(get_quota_used() or 0)
            cap = int(DAILY_QUOTA) or 1
            pct = 100.0 * used / cap
            if pct >= 95.0:
                alerts.append({
                    "severity": "critical",
                    "code": "yt_quota_near_cap",
                    "message": (
                        f"YouTube quota at {pct:.1f}% ({used}/{cap}). "
                        "Uploads will start failing within minutes."
                    ),
                })
        except Exception:
            # Don't add an alert here — quota readout failure is its own
            # signal but not 503-worthy (you can still upload manually).
            pass

        # SECRET_ENC_KEY missing AT RUNTIME (boot already checks this;
        # this catches "env got cleared after process start" — rare but
        # would silently corrupt every Fernet write going forward).
        if not (os.environ.get("SECRET_ENC_KEY") or "").strip():
            alerts.append({
                "severity": "critical",
                "code": "secret_enc_key_missing",
                "message": (
                    "SECRET_ENC_KEY is not set in process env. New writes "
                    "to the secret store would corrupt every existing "
                    "ciphertext until restored."
                ),
            })

        critical = [a for a in alerts if a["severity"] == "critical"]
        any_alert = bool(alerts)
        status_code = 503 if any_alert else 200
        return jsonify({
            "ok": not any_alert,
            "critical_count": len(critical),
            "alert_count": len(alerts),
            "alerts": alerts,
        }), status_code

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

    # Multi-tenant phase β: invitations + member-management routes.
    from blueprints.invitations import bp as invitations_bp
    from blueprints.members import bp as members_bp
    app.register_blueprint(invitations_bp)
    app.register_blueprint(members_bp)

    # Multi-tenant phase γ: 2FA, audit log, account recovery.
    from blueprints.twofa import bp as twofa_bp
    from blueprints.audit import bp as audit_bp
    from blueprints.recovery import bp as recovery_bp
    app.register_blueprint(twofa_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(recovery_bp)
    # Phase δ: stable user-facing agent download URLs.
    from blueprints.download import bp as download_bp
    app.register_blueprint(download_bp)
    # The /recover form is reachable without a session (a user who's lost
    # everything can't log in to ask for help). The reset POST is also
    # token-gated, not session-gated.
    _PUBLIC_ENDPOINTS.update({
        "recovery.recover_form", "recovery.recover_submit",
        "recovery.reset_form", "recovery.reset_submit",
    })

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

    # Rate-limit the recovery + auth endpoints that take user-controlled
    # input. /recover lets anyone submit a request for any username; we
    # already cap PER USER at 1/24h inside submit_request, but without
    # an IP cap an attacker can spam Owners' inboxes (phish fatigue) and
    # enumerate via timing side channels. /login is already covered by
    # auth.is_locked but a flask-limiter ceiling on top is cheap defense
    # in depth. Apply via the same view-functions trick as the agent
    # blueprint — limiter exists only after Limiter(app=...).
    for endpoint, limit in (
        ("recovery.recover_submit", "5 per minute"),
        ("recovery.reset_submit", "10 per hour"),
        ("auth.login_2fa_post", "10 per minute"),
        ("auth.login_email_2fa_post", "10 per minute"),
        ("auth.first_password_set_post", "10 per minute"),
    ):
        view = app.view_functions.get(endpoint)
        if view is None:
            # Blueprint not registered (e.g. test app skipping recovery) —
            # silently skip rather than fail-boot.
            continue
        app.view_functions[endpoint] = limiter.limit(limit)(view)

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

    # Phase γ: nightly audit_log archive job at 03:00 UTC.
    # Under TESTING we still build the scheduler so tests can inspect its
    # job table, but we do NOT start it — runaway background timers in CI
    # are a maintenance nightmare.
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from core.audit_archive import archive_old_entries
        sched = BackgroundScheduler(timezone="UTC")
        sched.add_job(
            archive_old_entries,
            trigger=CronTrigger(hour=3, minute=0, timezone="UTC"),
            id="audit_archive",
            replace_existing=True,
            max_instances=1,
        )
        # Suppress scheduler start under test runners (pytest sets
        # PYTEST_CURRENT_TEST in the worker process) or when the operator
        # explicitly disables it. Otherwise every test boot would start a
        # background timer thread and the suite shutdown would warn.
        _under_test = bool(
            os.environ.get("PYTEST_CURRENT_TEST")
            or os.environ.get("DLD_DISABLE_SCHEDULER")
            or app.config.get("TESTING")
        )
        if not _under_test:
            sched.start()
        app.config["scheduler"] = sched
    except Exception:
        app.logger.warning(
            "audit: APScheduler did not start; nightly archive will not run",
            exc_info=True,
        )

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
