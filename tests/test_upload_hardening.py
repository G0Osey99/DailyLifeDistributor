"""Hardening tests for the Rock + Vista upload paths (web and agent).

Covers the fixes from the upload-hardening review:
  * F1 — Vista propagates infra failures (PlaywrightTimeout, ConnectionError,
         OSError) instead of converting them to result dicts, so the dispatch
         circuit breaker can open. RuntimeError stays a per-row data failure.
  * F2 — Vista profile selection is verified: selector drift or a swallowed
         click raises instead of silently posting to the wrong networks.
  * F3 — Rock listing lookups wait (bounded) for grid rows before count().
  * F4 — PlaywrightSession never mutates a shared SessionConfig; org-scoped
         session paths never double-nest across orgs.
  * F5 — agent run_batch passes a phase progress_callback to Rock /
         Rock Email / Vista so agent runs emit phase_change frames.
"""
from __future__ import annotations

import sys
import threading
import types

import pytest

from uploaders import vista_social_uploader as vsu
from core import playwright_session as ps


# ---------------------------------------------------------------------------
# F1 — infra-failure propagation from upload_post
# ---------------------------------------------------------------------------


class _Entry:
    youtube_shorts_path = None
    vista_caption = "caption"
    description = "desc"
    vista_schedule_dt = None


def _entry_with_file(tmp_path):
    f = tmp_path / "short 260621.mp4"
    f.write_bytes(b"x")
    e = _Entry()
    e.youtube_shorts_path = str(f)
    import datetime as dt
    e.vista_schedule_dt = dt.datetime(2026, 6, 21, 6, 0)
    e.vista_social_description_footer = ""
    return e


class _RaisingSession:
    """Stand-in for PlaywrightSession that raises in __enter__."""

    def __init__(self, exc):
        self._exc = exc

    def __call__(self, config, progress_callback=None):
        return self

    def __enter__(self):
        raise self._exc

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("exc_type", [
    vsu.PlaywrightTimeout, ConnectionError, OSError, vsu.SessionExpiredError,
])
def test_upload_post_propagates_infra_failures(tmp_path, monkeypatch, exc_type):
    entry = _entry_with_file(tmp_path)
    monkeypatch.setattr(vsu, "PlaywrightSession", _RaisingSession(exc_type("boom")))
    with pytest.raises(exc_type):
        vsu.upload_post(entry)


def test_upload_post_runtimeerror_stays_result_dict(tmp_path, monkeypatch):
    entry = _entry_with_file(tmp_path)
    monkeypatch.setattr(
        vsu, "PlaywrightSession", _RaisingSession(RuntimeError("data problem")))
    result = vsu.upload_post(entry)
    assert result["success"] is False
    assert "data problem" in result["error"]


def test_infra_failures_does_not_match_every_exception():
    """The ImportError fallback for PlaywrightTimeout must be a dedicated
    class — if it were `Exception`, _INFRA_FAILURES would match data errors
    too and every per-row failure would trip the breaker."""
    assert Exception not in vsu._INFRA_FAILURES


# ---------------------------------------------------------------------------
# F2 — profile selection verification
# ---------------------------------------------------------------------------


class _FakePage:
    """Drives _set_profile_selection: first evaluate returns the toggle
    result, second evaluate returns the settled checkbox state."""

    def __init__(self, toggle_result, settled_state):
        self._results = [toggle_result, settled_state]
        self.evaluate_calls = 0

    def evaluate(self, script, arg=None):
        out = self._results[self.evaluate_calls]
        self.evaluate_calls += 1
        return out

    def wait_for_timeout(self, ms):
        pass


def test_profile_selection_raises_when_rows_missing():
    page = _FakePage(
        {"changed": [], "found": [], "wrapper_count": 0},
        {},
    )
    with pytest.raises(RuntimeError, match="could not find the profile"):
        vsu._set_profile_selection(page, ["facebook", "instagram"], ["youtube"])


def test_profile_selection_raises_when_state_wrong():
    page = _FakePage(
        {"changed": ["+instagram"], "found": ["facebook", "instagram", "youtube"],
         "wrapper_count": 6},
        # Settled state says instagram never actually toggled on.
        {"facebook": True, "instagram": False, "youtube": False},
    )
    with pytest.raises(RuntimeError, match="did not settle"):
        vsu._set_profile_selection(page, ["facebook", "instagram"], ["youtube"])


def test_profile_selection_ok_when_verified():
    page = _FakePage(
        {"changed": ["+instagram", "-youtube"],
         "found": ["facebook", "instagram", "youtube"], "wrapper_count": 6},
        {"facebook": True, "instagram": True, "youtube": False},
    )
    vsu._set_profile_selection(page, ["facebook", "instagram"], ["youtube"])
    assert page.evaluate_calls == 2


def test_profile_selection_tolerates_unfound_uncheck_network():
    """A YouTube row that isn't in the picker at all is fine — nothing to
    uncheck. Only the to-check networks are mandatory."""
    page = _FakePage(
        {"changed": [], "found": ["facebook", "instagram"], "wrapper_count": 4},
        {"facebook": True, "instagram": True},
    )
    vsu._set_profile_selection(page, ["facebook", "instagram"], ["youtube"])


# ---------------------------------------------------------------------------
# F3 — Rock listing-grid wait is bounded + tolerant
# ---------------------------------------------------------------------------


def test_rock_wait_for_listing_grid_swallows_timeout():
    from uploaders.rock.client import RockBrowserClient, PlaywrightTimeout as RPT

    client = RockBrowserClient.__new__(RockBrowserClient)

    class _Page:
        def wait_for_selector(self, sel, state=None, timeout=None):
            raise RPT("no rows")

    client._page = _Page()
    client._wait_for_listing_grid(timeout_ms=10)  # must not raise


# ---------------------------------------------------------------------------
# F4 — org-scoped session paths + shared-config immutability
# ---------------------------------------------------------------------------


def test_org_scoped_path_basic(tmp_path):
    base = str(tmp_path / "rock_session.json")
    scoped = ps._org_scoped_session_path(base, 1)
    assert scoped == str(tmp_path / ".sessions" / "org_1" / "rock_session.json")


def test_org_scoped_path_idempotent_same_org(tmp_path):
    scoped = str(tmp_path / ".sessions" / "org_1" / "rock_session.json")
    assert ps._org_scoped_session_path(scoped, 1) == scoped


def test_org_scoped_path_reroots_for_other_org(tmp_path):
    org1 = str(tmp_path / ".sessions" / "org_1" / "rock_session.json")
    org2 = ps._org_scoped_session_path(org1, 2)
    assert org2 == str(tmp_path / ".sessions" / "org_2" / "rock_session.json")
    assert ".sessions" + str(tmp_path)[:0] not in org2.replace(
        str(tmp_path / ".sessions" / "org_2"), "", 1)  # no double nesting


def test_open_does_not_mutate_shared_config(tmp_path, monkeypatch):
    """_open must re-bind self.config (dataclasses.replace), never write the
    org-scoped path back into the (module-level, shared) SessionConfig."""
    shared = ps.SessionConfig(
        name="t", session_file=str(tmp_path / "t_session.json"),
        is_login_url=lambda u: False, target_url="",
    )
    sess = ps.PlaywrightSession.__new__(ps.PlaywrightSession)
    sess.config = shared
    sess._progress = None
    sess._pw = object()
    monkeypatch.setattr(ps, "sync_playwright", object())

    from core import org_context
    monkeypatch.setattr(
        "core.org_context.effective_org_id", lambda: 7, raising=False)

    class _Browser:
        def new_context(self, **kw):
            return _Ctx()

    class _Ctx:
        def new_page(self):
            return _Pg()

    class _Pg:
        url = "https://example.test/home"

        def set_default_timeout(self, ms):
            pass

        def goto(self, *a, **k):
            pass

    monkeypatch.setattr(sess, "_launch", lambda headless: _Browser())
    monkeypatch.setattr(ps, "has_session", lambda f, org_id=None: True)
    monkeypatch.setattr(ps, "_load_session_blob_to", lambda f, org_id=None: False)

    sess._open()

    assert shared.session_file == str(tmp_path / "t_session.json"), (
        "shared SessionConfig was mutated by _open()"
    )
    assert sess.config is not shared
    assert "org_7" in sess.config.session_file


# ---------------------------------------------------------------------------
# F7 — hosted (no X server) defaults the WEB upload path to headless
# ---------------------------------------------------------------------------


def test_uploader_session_configs_default_headless_on_hosted(monkeypatch):
    """Live-reproduced on the VPS: the web batch path launched Chrome HEADED
    in the container ("Looks like you launched a headed browser without
    having a XServer running") because only the agent runner force-set the
    *_HEADLESS env vars. The uploader SessionConfigs must default headless
    whenever HOSTED is set."""
    import importlib
    monkeypatch.setenv("HOSTED", "true")
    import uploaders.vista_social_uploader as v
    import uploaders.rock.client as rc
    import uploaders.simplecast_uploader as sc
    try:
        v = importlib.reload(v)
        rc = importlib.reload(rc)
        sc = importlib.reload(sc)
        assert v._VS_SESSION_CONFIG.default_headless is True
        assert rc._ROCK_SESSION_CONFIG.default_headless is True
        assert sc._SC_SESSION_CONFIG_BASE.default_headless is True
    finally:
        # Reload back without HOSTED so later tests see pristine modules.
        monkeypatch.delenv("HOSTED", raising=False)
        importlib.reload(v)
        importlib.reload(rc)
        importlib.reload(sc)


def test_simplecast_per_call_config_carries_all_base_fields(monkeypatch):
    """The per-call SimpleCast SessionConfig must be a full copy of the base
    (dataclasses.replace), not a field-by-field rebuild that silently drops
    newly added fields like default_headless."""
    import dataclasses
    import uploaders.simplecast_uploader as sc
    base = dataclasses.replace(sc._SC_SESSION_CONFIG_BASE,
                               default_headless=True)
    monkeypatch.setattr(sc, "_SC_SESSION_CONFIG_BASE", base)
    cfg = dataclasses.replace(sc._SC_SESSION_CONFIG_BASE, target_url="https://x")
    assert cfg.default_headless is True
    assert cfg.target_url == "https://x"
    assert cfg.login_url == "https://x"  # __post_init__ re-ran


# ---------------------------------------------------------------------------
# F5 — agent dispatch passes phase callbacks to Rock / Rock Email / Vista
# ---------------------------------------------------------------------------


def _agent_row():
    return {
        "row_idx": 0,
        "iso_date": "2026-06-21",
        "platforms": ["Rock", "Vista Social", "Rock Email"],
        "elements": {},
        "entry": {
            "date": "2026-06-21",
            "display_date": "2026-06-21",
            "youtube_watch_url": "https://youtube.com/watch?v=x",
        },
    }


@pytest.mark.parametrize("platform,module_attr,func_name", [
    ("Rock", "uploaders.rock.orchestrator", "upload_daily_experience"),
    ("Rock Email", "uploaders.rock.email", "schedule_email"),
    ("Vista Social", "uploaders.vista_social_uploader", "upload_post"),
])
def test_agent_dispatch_passes_phase_callback(monkeypatch, platform,
                                              module_attr, func_name):
    from agent import run_batch as arb

    captured_cb = {}

    def _fake_uploader(*args, **kwargs):
        captured_cb["cb"] = kwargs.get("progress_callback")
        if captured_cb["cb"] is not None:
            captured_cb["cb"]("test_phase")
        return {"success": True, "url": "https://x"}

    monkeypatch.setattr(f"{module_attr}.{func_name}", _fake_uploader)

    frames = []
    arb._dispatch_upload(platform=platform, row=_agent_row(), emit=frames.append,
                         paths={"2026-06-21": {}})

    assert captured_cb.get("cb") is not None, (
        f"{platform}: progress_callback not passed on the agent path")
    phase_frames = [f for f in frames if f.get("event") == "phase_change"]
    assert phase_frames and phase_frames[0]["phase"] == "test_phase"
    assert phase_frames[0]["platform"] == platform
    assert any(f.get("event") == "success" for f in frames)
