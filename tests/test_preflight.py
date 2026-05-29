"""Tests for the pre-upload readiness check (blueprints/preflight)."""
from __future__ import annotations

import pytest


@pytest.fixture
def _patch_env(monkeypatch):
    """Neutralize external deps so each test controls exactly one signal."""
    from uploaders import youtube_uploader
    from core import secrets_store
    from core import llm_title_gen
    import blueprints.preflight as pf

    # Defaults: YouTube authed, all sessions present, LLM up, no image key.
    monkeypatch.setattr(youtube_uploader, "is_authenticated", lambda: True)
    monkeypatch.setattr(secrets_store, "has_secret",
                        lambda name, org_id=None: True)
    monkeypatch.setattr(secrets_store, "get_blob",
                        lambda name, org_id=None: b"{}")
    monkeypatch.setattr(secrets_store, "get_secret",
                        lambda name, org_id=None: None)
    monkeypatch.setattr(llm_title_gen, "is_llamafile_running", lambda: True)
    # Clear image-provider keys from the real env so the "no key" default
    # holds regardless of what's set on the host / leaked by another test.
    monkeypatch.delenv("UNSPLASH_ACCESS_KEY", raising=False)
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    # Stop the LLM model-list probe from doing real network I/O.
    monkeypatch.setattr(pf, "_check_llm",
                        lambda: {"ok": True, "status": "Reachable",
                                 "detail": "", "blocking": False})
    return monkeypatch


def test_all_green_when_everything_ready(_patch_env):
    from blueprints.preflight import run_preflight
    res = run_preflight(org_id=1)
    assert res["ok"] is True
    assert res["checks"]["youtube"]["ok"] is True
    assert res["checks"]["rock"]["ok"] is True


def test_missing_youtube_token_fails_blocking(_patch_env):
    from uploaders import youtube_uploader
    _patch_env.setattr(youtube_uploader, "is_authenticated", lambda: False)
    from blueprints.preflight import run_preflight
    res = run_preflight(platforms=["youtube_video"], org_id=1)
    assert res["ok"] is False
    assert res["checks"]["youtube"]["ok"] is False
    assert res["checks"]["youtube"]["blocking"] is True
    assert "Settings" in res["checks"]["youtube"]["detail"]


def test_missing_session_fails_blocking(_patch_env):
    from core import secrets_store
    _patch_env.setattr(secrets_store, "has_secret", lambda name, org_id=None: False)
    _patch_env.setattr(secrets_store, "get_blob", lambda name, org_id=None: None)
    from blueprints.preflight import run_preflight
    res = run_preflight(platforms=["rock"], org_id=1)
    assert res["ok"] is False
    assert res["checks"]["rock"]["ok"] is False
    assert "Connect Rock" in res["checks"]["rock"]["detail"]


def test_non_blocking_llm_failure_does_not_fail_overall(_patch_env):
    """A down LLM is a warning, not a blocker — ok must stay True."""
    import blueprints.preflight as pf
    _patch_env.setattr(pf, "_check_llm",
                       lambda: {"ok": False, "status": "Unreachable",
                                "detail": "down", "blocking": False})
    from blueprints.preflight import run_preflight
    res = run_preflight(platforms=["youtube_video"], org_id=1)
    assert res["checks"]["llm"]["ok"] is False
    assert res["ok"] is True, "non-blocking LLM failure must not gate ok"


def test_platforms_filter_limits_checks(_patch_env):
    from blueprints.preflight import run_preflight
    res = run_preflight(platforms=["simplecast"], org_id=1)
    assert "simplecast" in res["checks"]
    assert "youtube" not in res["checks"]
    assert "rock" not in res["checks"]


def test_image_provider_surfaced_when_rock_in_run(_patch_env):
    from blueprints.preflight import run_preflight
    res = run_preflight(platforms=["rock"], org_id=1)
    # No image key set in the fixture → non-blocking warning present.
    assert res["checks"]["image_provider"]["ok"] is False
    assert res["checks"]["image_provider"]["blocking"] is False
    # ...but ok stays True since it's non-blocking and the Rock session is present.
    assert res["ok"] is True


# ---------------------------------------------------------------------------
# validate_run — per-row data dry-run
# ---------------------------------------------------------------------------
def _scan(shorts="app 260601.mp4", video="youtube 260601.mp4",
          podcast="podcast 260601.mp3", meta=None):
    cats = {}
    if video:
        cats["youtube_video"] = [video]
    if shorts:
        cats["youtube_shorts"] = [shorts]
    if podcast:
        cats["podcast"] = [podcast]
    return {"2026-06-01": {"categories": cats, "metadata": meta or {}}}


def test_dryrun_all_good_for_simple_platforms():
    from blueprints.preflight import validate_run
    # SimpleCast needs a resolvable episode title (podcast_title/youtube_title);
    # files alone aren't enough (CAL-1).
    res = validate_run(["2026-06-01"], ["youtube_video", "youtube_shorts", "simplecast"],
                       _scan(meta={"youtube_title": "Daily Life June 1"}))
    assert res["ok"] is True
    assert all(r["ok"] for r in res["rows"])


def test_dryrun_simplecast_flags_missing_title():
    """CAL-1: podcast audio present but no title → false-GREEN previously."""
    from blueprints.preflight import validate_run
    res = validate_run(["2026-06-01"], ["simplecast"], _scan(meta={}))
    row = res["rows"][0]
    assert row["ok"] is False
    assert any("title" in i for i in row["issues"])


def test_dryrun_flags_missing_shorts_file():
    from blueprints.preflight import validate_run
    res = validate_run(["2026-06-01"], ["youtube_shorts"], _scan(shorts=None))
    row = res["rows"][0]
    assert row["ok"] is False
    assert any("Shorts" in i for i in row["issues"])
    assert res["ok"] is False


def test_dryrun_rock_flags_missing_wistia_and_fields():
    from blueprints.preflight import validate_run
    # Shorts filename with no date code → wistia can't infer; no Excel fields.
    res = validate_run(["2026-06-01"], ["rock"],
                       _scan(shorts="clip.mp4", meta={}))
    row = res["rows"][0]
    assert row["ok"] is False
    joined = " ".join(row["issues"])
    assert "Wistia" in joined
    assert "passage" in joined and "scripture" in joined and "prayer" in joined


def test_dryrun_rock_passes_with_full_data():
    from blueprints.preflight import validate_run
    meta = {"episode_title": "Ep", "passage": "Acts 1:1",
            "scripture": "text", "prayer": "pray"}
    res = validate_run(["2026-06-01"], ["rock"],
                       _scan(shorts="app 260601.mp4", meta=meta))
    assert res["rows"][0]["ok"] is True


def test_dryrun_quota_warns_when_over_daily_cap(monkeypatch):
    """validate_run must estimate YouTube quota and warn when the run won't
    fit. Cost-agnostic: we pin a low cap so the test doesn't break when
    Google changes the per-upload cost (it dropped 1600→100 on 2025-12-04)."""
    from blueprints import preflight
    from core import quota
    monkeypatch.setattr(quota, "DAILY_QUOTA", 200)  # tiny cap → easily exceeded
    monkeypatch.setattr(quota, "get_quota_used", lambda: 0)
    dates = [f"2026-06-{d:02d}" for d in range(1, 11)]
    scan = {d: {"categories": {"youtube_video": [f"yt {d}.mp4"]}, "metadata":
                {"youtube_title": "T"}} for d in dates}
    res = preflight.validate_run(dates, ["youtube_video"], scan)
    q = res["quota"]
    assert q["youtube_uploads"] == 10
    assert q["fits"] is False
    assert "quota" in q["message"].lower()


def test_dryrun_quota_fits_a_full_month_at_current_cost(monkeypatch):
    """At the current 100-unit videos.insert cost, a full month of dates
    (Video + Shorts) fits the default 10,000/day cap — the user's real case."""
    from blueprints import preflight
    from core import quota
    monkeypatch.setattr(quota, "DAILY_QUOTA", 10000)
    monkeypatch.setattr(quota, "get_quota_used", lambda: 0)
    dates = [f"2026-06-{d:02d}" for d in range(1, 31)]  # 30 dates
    scan = {d: {"categories": {"youtube_video": [f"yt {d}.mp4"],
                               "youtube_shorts": [f"app {d}.mp4"]},
                "metadata": {"youtube_title": "T"}} for d in dates}
    res = preflight.validate_run(dates, ["youtube_video", "youtube_shorts"], scan)
    q = res["quota"]
    assert q["youtube_uploads"] == 60  # 30 dates × (video + shorts)
    assert q["fits"] is True, f"30 dates should fit at current cost: {q}"


def test_dryrun_rock_email_needs_youtube_video_in_run():
    from blueprints.preflight import validate_run
    # Rock Email alone, no YouTube Video → no watch URL source.
    res = validate_run(["2026-06-01"], ["rock_email"], _scan())
    assert res["rows"][0]["ok"] is False
    assert any("watch URL" in i for i in res["rows"][0]["issues"])
    # With YouTube Video in the run, the URL source is satisfied.
    res2 = validate_run(["2026-06-01"], ["rock_email", "youtube_video"], _scan())
    email_row = [r for r in res2["rows"] if r["platform"] == "Rock Email"][0]
    assert email_row["ok"] is True


def test_route_requires_auth_returns_json(_patch_env):
    """The route is behind the app auth gate; an unauthenticated JSON
    request gets 401, not a redirect."""
    import importlib
    app_mod = importlib.import_module("app")
    application = app_mod.create_app()
    application.config.update(TESTING=True)
    client = application.test_client()
    r = client.get("/preflight/check",
                   headers={"Accept": "application/json"})
    assert r.status_code == 401
