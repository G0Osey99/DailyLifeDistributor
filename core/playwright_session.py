"""Shared Playwright launch + session-state plumbing for the browser uploaders.

The three Playwright-driven uploaders (SimpleCast, Vista Social, Rock) all
follow the same skeleton:

  1. Launch the system Google Chrome via Playwright (`channel='chrome'`,
     or `executable_path` from a per-service env var).
  2. Open a new context, optionally restoring `storage_state` from a
     project-root JSON file that travels on the USB.
  3. Navigate to a service URL. If we land on a login page, prompt the
     user to log in manually (always headed for that step) and persist
     the resulting cookies + local storage.
  4. On every subsequent run, re-save the storage_state so the cookie's
     sliding expiry doesn't age out across runs.

Before this module each uploader re-implemented all of the above with
slightly different login-detection regex, slightly different timeouts,
and slightly different cleanup paths. Centralising the skeleton means a
fix to login detection, a tweak to launch flags, or a cleanup bug is
written once and inherited by all three.

Each caller provides a :class:`SessionConfig` describing its specifics:

  - filenames + login URL
  - "is this a login page?" callable (each service has its own redirect)
  - environment variable names (HEADLESS / LOGIN_TIMEOUT / CHROME_PATH)

The shared :class:`PlaywrightSession` context manager handles the rest.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

try:
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Page,
        sync_playwright,
        TimeoutError as PlaywrightTimeout,
    )
except ImportError:  # pragma: no cover - playwright optional at import time
    Browser = BrowserContext = Page = object  # type: ignore[misc,assignment]
    sync_playwright = None  # type: ignore[assignment]
    PlaywrightTimeout = Exception  # type: ignore[assignment,misc]


log = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_MS = 30_000
_DEFAULT_LOGIN_TIMEOUT_S = 300


# ---------------------------------------------------------------------------
# Encrypted-store helpers for Playwright session blobs
# ---------------------------------------------------------------------------


def _session_secret_name(session_file: str) -> str:
    """Return the secrets-store key for a given session file path."""
    base = os.path.splitext(os.path.basename(session_file))[0]
    return f"playwright.{base}"


def _load_session_blob_to(session_file: str) -> bool:
    """Write the stored encrypted session to session_file. Returns True if found."""
    from core import secrets_store
    data = secrets_store.get_blob(_session_secret_name(session_file))
    if data is None:
        return False
    with open(session_file, "wb") as f:
        f.write(data)
    if os.name != "nt":
        os.chmod(session_file, 0o600)
    return True


def _persist_session_blob(session_file: str) -> None:
    """Read session_file back into the encrypted store after a save."""
    if not os.path.exists(session_file):
        return
    from core import secrets_store
    with open(session_file, "rb") as f:
        secrets_store.set_blob(_session_secret_name(session_file), f.read())


def has_session(session_file: str) -> bool:
    """True if a saved session exists in the store or on disk."""
    from core import secrets_store
    return secrets_store.has_secret(_session_secret_name(session_file)) or os.path.exists(session_file)


def clear_session(session_file: str) -> None:
    """Clear a saved session (disk first, then store).

    Removes the on-disk file first so a locked file (e.g. Chrome still open)
    raises before we delete the store copy — avoiding a split state where the
    store is cleared but a stale file remains that _open would still use.
    """
    if os.path.exists(session_file):
        os.remove(session_file)  # may raise OSError if locked — let it propagate
    from core import secrets_store
    secrets_store.delete_secret(_session_secret_name(session_file))


@dataclass
class SessionConfig:
    """Per-service launch parameters.

    Only ``name``, ``session_file``, and ``is_login_url`` are mandatory;
    everything else has sensible defaults.
    """

    name: str
    session_file: str
    is_login_url: Callable[[str], bool]
    # The URL we want to land on once logged in. The caller usually
    # navigates here; we fall back to it after a fresh login if the post-
    # auth redirect lands somewhere else.
    target_url: str = ""
    # Where to send the user when their session is missing or expired.
    # Falls back to ``target_url`` (most dashboards bounce anonymous
    # callers there to a login page, which is the moral equivalent).
    login_url: str = ""
    # Env var that toggles headless once a session exists. First-time
    # login is *always* headed regardless of this value.
    headless_env: str = ""
    # Headless mode used when the env var above is unset/empty. Refresh
    # sources flip this to True (non-interactive scrapes shouldn't pop a
    # window every time); the uploader keeps it False so saved-session
    # runs stay visible by default.
    default_headless: bool = False
    # Env var holding the manual-login deadline in seconds.
    login_timeout_env: str = ""
    default_login_timeout: int = _DEFAULT_LOGIN_TIMEOUT_S
    # Env var pointing at a non-default Chrome binary.
    chrome_path_env: str = ""
    # Optional viewport override forwarded to ``new_context``.
    viewport: Optional[dict] = None
    # Default per-action timeout applied to the page.
    default_timeout_ms: int = _DEFAULT_TIMEOUT_MS
    # When True, refuse to fall back to a headed manual-login flow if the
    # saved session is missing or expired — raise :class:`SessionExpiredError`
    # instead. Used by non-interactive callers (the calendar-refresh sources)
    # that have no human at the keyboard to type a password.
    no_login_recovery: bool = False

    def __post_init__(self) -> None:
        if not self.login_url:
            self.login_url = self.target_url


@dataclass
class _LaunchedSession:
    """Internal handle: the live objects we hand back to the caller."""
    browser: "Browser"
    context: "BrowserContext"
    page: "Page"


# Public phase strings — kept stable so SSE consumers can switch on them.
PHASE_LAUNCHING = "launching"
PHASE_AWAITING_LOGIN = "awaiting_login"
PHASE_NAVIGATING = "navigating"


class SessionExpiredError(RuntimeError):
    """Saved Playwright session is no longer valid and headless cannot recover."""


def _atomic_save_storage_state(context, target_path: str) -> None:
    """Save Playwright storage_state to ``target_path`` atomically.

    Why: the session JSON lives on the USB drive. A non-atomic write that
    gets interrupted (USB unplug, Chrome crash mid-save) truncates the file
    and forces the next run through the manual-login path. Writing to a
    sibling temp file and ``os.replace``-ing makes the swap atomic on the
    same filesystem, so the worst case becomes "we still have the previous
    session" instead of "session file is broken JSON".
    """
    tmp_path = f"{target_path}.tmp"
    try:
        context.storage_state(path=tmp_path)
        os.replace(tmp_path, target_path)
    except Exception:
        # Best-effort tmp cleanup; never let cleanup mask the original error.
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise


class PlaywrightSession:
    """Context manager: yields a logged-in (browser, context, page).

    Usage::

        cfg = SessionConfig(name="simplecast", session_file=..., ...)
        with PlaywrightSession(cfg, progress_callback=cb) as sess:
            sess.page.goto(...)
            ...

    On exit the session is re-saved (so sliding cookies refresh on disk),
    then the browser is closed. If the caller raised, cleanup still runs.
    """

    def __init__(
        self,
        config: SessionConfig,
        *,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        if sync_playwright is None:
            raise RuntimeError(
                "playwright is not installed — run: pip install playwright"
            )
        self.config = config
        self._progress = progress_callback
        self._pw = None
        self.browser: Optional["Browser"] = None
        self.context: Optional["BrowserContext"] = None
        self.page: Optional["Page"] = None

    # -- public api -------------------------------------------------------

    def __enter__(self) -> "PlaywrightSession":
        self._pw = sync_playwright().start()
        try:
            self._open()
        except BaseException:
            # If launch/auth fails, make sure we don't leak the Playwright
            # process — __exit__ won't run on a failed __enter__.
            self._safe_stop_pw()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            # Re-save the session if we have one — cookies often slide-
            # expire and we want every successful run to refresh them on
            # disk. Skip when Chrome is already torn down.
            if self.context is not None:
                try:
                    _atomic_save_storage_state(self.context, self.config.session_file)
                    _persist_session_blob(self.config.session_file)
                    # Store now holds the latest session; don't leave plaintext on disk.
                    try:
                        if os.path.exists(self.config.session_file):
                            os.remove(self.config.session_file)
                    except OSError:
                        pass
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "%s: storage_state save on exit failed: %s",
                        self.config.name, e,
                    )
        finally:
            # M3: cleanup must never raise, but a silent swallow makes Chrome
            # zombie processes hard to diagnose. Debug-log instead of pass.
            try:
                if self.page is not None:
                    self.page.close()
            except Exception as e:
                log.debug("%s: page.close failed: %s", self.config.name, e)
            try:
                if self.context is not None:
                    self.context.close()
            except Exception as e:
                log.debug("%s: context.close failed: %s", self.config.name, e)
            try:
                if self.browser is not None:
                    self.browser.close()
            except Exception as e:
                # Browser leaks are the real zombie risk (page/context close
                # failures after browser teardown are normal and stay at debug).
                log.warning("%s: browser.close failed: %s", self.config.name, e)
            self._safe_stop_pw()

    # -- internals --------------------------------------------------------

    def _emit(self, phase: str) -> None:
        if self._progress is None:
            return
        try:
            self._progress(phase)
        except Exception:
            # M4: progress callback failures are intentionally non-fatal,
            # but log so a consistently-broken callback isn't invisible.
            log.debug("%s: progress callback raised on phase %s", self.config.name, phase, exc_info=True)

    def _safe_stop_pw(self) -> None:
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception as e:
                log.debug("%s: playwright stop failed: %s", self.config.name, e)
            self._pw = None

    def _launch(self, *, headless: bool) -> "Browser":
        assert self._pw is not None
        kwargs: dict = {"headless": headless}
        chrome_path = ""
        if self.config.chrome_path_env:
            chrome_path = (os.environ.get(self.config.chrome_path_env, "") or "").strip()
        if chrome_path:
            kwargs["executable_path"] = chrome_path
        else:
            kwargs["channel"] = "chrome"
        try:
            return self._pw.chromium.launch(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"{self.config.name}: could not launch Chrome: {exc}. Make "
                "sure Google Chrome is installed at the standard location, "
                "or set "
                f"{self.config.chrome_path_env or 'a chrome path env var'} "
                "to point at the Chrome binary."
            )

    def _new_context(self, *, with_session: bool) -> "BrowserContext":
        assert self.browser is not None
        ctx_kwargs: dict = {}
        if with_session:
            _load_session_blob_to(self.config.session_file)
        if with_session and os.path.isfile(self.config.session_file):
            ctx_kwargs["storage_state"] = self.config.session_file
        if self.config.viewport is not None:
            ctx_kwargs["viewport"] = self.config.viewport
        try:
            return self.browser.new_context(**ctx_kwargs)
        except Exception as e:
            # M27: a malformed session JSON used to surface as an opaque
            # Playwright error from __enter__. If we were trying to load a
            # session file, retry without it and prompt the user.
            if "storage_state" in ctx_kwargs:
                log.warning(
                    "%s: failed to load session file %s (%s) — retrying without it; "
                    "user will need to log in again.",
                    self.config.name, self.config.session_file, e,
                )
                ctx_kwargs.pop("storage_state", None)
                return self.browser.new_context(**ctx_kwargs)
            raise

    def _headless_pref(self) -> bool:
        if not self.config.headless_env:
            return self.config.default_headless
        raw = os.environ.get(self.config.headless_env, "").strip().lower()
        if not raw:
            return self.config.default_headless
        return raw == "true"

    def _login_timeout_seconds(self) -> int:
        if not self.config.login_timeout_env:
            return self.config.default_login_timeout
        raw = os.environ.get(self.config.login_timeout_env, "").strip()
        if not raw:
            return self.config.default_login_timeout
        try:
            return int(raw)
        except ValueError:
            return self.config.default_login_timeout

    def _open(self) -> None:
        """Land on a logged-in page, prompting the user if needed."""
        self._emit(PHASE_LAUNCHING)

        have_session = has_session(self.config.session_file)
        # Non-interactive callers can't recover from a missing session —
        # bail out cleanly so the orchestrator surfaces a re-login prompt
        # instead of the user staring at a never-completing browser launch.
        if not have_session and self.config.no_login_recovery:
            raise SessionExpiredError(
                f"{self.config.name}: session file missing "
                f"({self.config.session_file})"
            )

        # Always headed for the first-ever login — there's no way for
        # the user to type their password into a hidden window. Non-
        # interactive callers (no_login_recovery) honor headless_pref
        # regardless of session state because they raise on a login
        # redirect rather than waiting for typing.
        if self.config.no_login_recovery:
            headless = self._headless_pref()
        else:
            headless = self._headless_pref() if have_session else False

        self.browser = self._launch(headless=headless)
        self.context = self._new_context(with_session=have_session)
        self.page = self.context.new_page()
        self.page.set_default_timeout(self.config.default_timeout_ms)

        if self.config.target_url:
            self._emit(PHASE_NAVIGATING)
            self.page.goto(
                self.config.target_url,
                wait_until="domcontentloaded",
                timeout=self.config.default_timeout_ms,
            )

        if self._on_login_page():
            if self.config.no_login_recovery:
                raise SessionExpiredError(
                    f"{self.config.name}: redirected to login page "
                    f"({self.page.url if self.page else '?'})"
                )
            self._handle_login()

    def _on_login_page(self) -> bool:
        try:
            return bool(self.page and self.config.is_login_url(self.page.url or ""))
        except Exception:
            return False

    def _handle_login(self) -> None:
        """Drop into a headed window and wait for the user to authenticate."""
        assert self.page is not None
        # If we ended up here while headless, relaunch headed — the user
        # can't type into an invisible browser. We deliberately drop the
        # stale session state when relaunching: the cookie that put us
        # on a login page won't help on the next attempt.
        if not self._headed_now():
            log.info(
                "%s: session expired; relaunching headed for manual login",
                self.config.name,
            )
            self._relaunch_headed()

        self._emit(PHASE_AWAITING_LOGIN)
        log.info(
            "%s: waiting for the user to log in (timeout %ds)",
            self.config.name, self._login_timeout_seconds(),
        )

        # Some services land directly on /login already; others want a
        # nudge. Goto the configured login URL only if we aren't already
        # there.
        if self.config.login_url and not (self.page.url or "").startswith(
            self.config.login_url
        ):
            try:
                self.page.goto(
                    self.config.login_url,
                    wait_until="domcontentloaded",
                    timeout=self.config.default_timeout_ms,
                )
            except Exception:
                pass

        self._wait_for_login()

        # Persist immediately — a crash later in the run shouldn't force
        # the user to log in again.
        if self.context is not None:
            try:
                _atomic_save_storage_state(self.context, self.config.session_file)
                _persist_session_blob(self.config.session_file)
                log.info(
                    "%s: session saved to %s",
                    self.config.name, self.config.session_file,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: could not save session file: %s",
                            self.config.name, exc)

        # After login we may have landed somewhere else; bring the page
        # back to the desired target so the caller can pick up.
        if self.config.target_url and self.page is not None:
            if self.config.target_url not in (self.page.url or ""):
                self.page.goto(
                    self.config.target_url,
                    wait_until="domcontentloaded",
                    timeout=self.config.default_timeout_ms,
                )

        if self._on_login_page():
            raise RuntimeError(
                f"{self.config.name}: still on a login page after login. "
                f"Try deleting {self.config.session_file} and retrying."
            )

    def _headed_now(self) -> bool:
        """Best-effort check that the current browser is visible.

        Playwright does not expose the launch flag back, so we shadow it
        by re-reading the env preference. Good enough — if we get this
        wrong we just relaunch unnecessarily.
        """
        return not self._headless_pref()

    def _relaunch_headed(self) -> None:
        # Tear down the current session-bearing context entirely. The
        # cookies that put us on /login aren't worth carrying forward.
        try:
            if self.page is not None:
                self.page.close()
        except Exception:
            pass
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:
            pass
        self.browser = self._launch(headless=False)
        self.context = self._new_context(with_session=False)
        self.page = self.context.new_page()
        self.page.set_default_timeout(self.config.default_timeout_ms)

    def _wait_for_login(self) -> None:
        assert self.page is not None
        deadline = time.time() + self._login_timeout_seconds()
        while time.time() < deadline:
            try:
                if not self._on_login_page():
                    # Small settle so any post-login redirects resolve
                    # before we re-check.
                    self.page.wait_for_timeout(1500)
                    if not self._on_login_page():
                        return
            except Exception as e:
                # M4: log so a pathological exception loop is diagnosable.
                log.debug("%s: login-page check raised: %s", self.config.name, e)
            try:
                self.page.wait_for_timeout(1000)
            except Exception as e:
                # Page navigating / closing — just spin and retry.
                log.debug("%s: wait_for_timeout raised during login wait: %s", self.config.name, e)
                time.sleep(1.0)
        raise RuntimeError(
            f"{self.config.name}: timed out after "
            f"{self._login_timeout_seconds()}s waiting for login. Rerun and "
            "log in sooner, or raise the timeout via the configured env var."
        )


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


def emit_phase(cb: Optional[Callable[[str], None]], phase: str) -> None:
    """Best-effort progress callback. Swallows callback errors so a buggy
    listener never aborts an upload mid-flight.
    """
    if cb is None:
        return
    try:
        cb(phase)
    except Exception:
        pass


def url_marker_login_check(markers: tuple[str, ...]) -> Callable[[str], bool]:
    """Return an ``is_login_url`` callable that matches any of the given
    case-insensitive substrings — covers the SimpleCast / Vista pattern.
    """
    lowered = tuple(m.lower() for m in markers)

    def _check(url: str) -> bool:
        u = (url or "").lower()
        return any(m in u for m in lowered)

    return _check


@contextmanager
def open_session(
    config: SessionConfig,
    *,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Iterator[PlaywrightSession]:
    """Functional convenience wrapper for callers that prefer ``with open_session(...)``.

    Equivalent to ``with PlaywrightSession(config, ...) as sess: yield sess``.
    """
    sess = PlaywrightSession(config, progress_callback=progress_callback)
    with sess:
        yield sess
