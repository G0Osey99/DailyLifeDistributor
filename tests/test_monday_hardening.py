"""Regression tests pinning the fixes from the Monday-hardening pass.

Each test here corresponds to a production failure that bit a real upload
run and was fixed under time pressure. Without these, a future refactor
could silently reintroduce any of them — and the symptom only shows up
during a live upload, which is the worst possible place to discover it.

Covered:
  * YouTube 308 strip — get_authenticated_service must remove 308 from the
    httplib2 redirect_codes, or every resumable upload chunk fails with
    "Redirected but the response is missing a Location: header."
  * Wistia ref via hex-UUID temp path — the web path reassembles uploads
    into a UUID filename, so build_entry must infer the Wistia ref from the
    ORIGINAL filename (youtube_shorts_name), not the temp path.
  * image_gatherer LLM model — the chat-completions POST must use LLM_MODEL
    (e.g. "llama3.2" on Ollama), not the hardcoded "local" that Ollama 404s.
  * Vista Social visibility — an enabled Vista Social platform must produce
    a summary row even when the Shorts file is missing, so the failure is
    visible instead of the row silently vanishing.
"""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# YouTube 308 strip
# ---------------------------------------------------------------------------
def test_get_authenticated_service_strips_308_from_redirect_codes(monkeypatch):
    """The Http handed to googleapiclient.build must NOT treat 308 as a
    redirect. YouTube resumable uploads use 308 'Resume Incomplete' with a
    Range header and no Location; httplib2's default redirect handling then
    raises RedirectMissingLocation on the first chunk."""
    import httplib2
    from uploaders import youtube_uploader as yt

    # Skip if the optional google-auth-httplib2 dep isn't importable — the
    # 308 strip only lives on that branch; the fallback path delegates to
    # googleapiclient.build_http() which strips 308 itself.
    pytest.importorskip("google_auth_httplib2")

    # A creds object that is already valid so the refresh/OAuth branches are
    # skipped entirely.
    class _FakeCreds:
        valid = True
        expired = False
        refresh_token = None
        scopes = ["https://www.googleapis.com/auth/youtube"]

        def to_json(self):
            return "{}"

    monkeypatch.setattr(yt, "_load_token_json", lambda: json.dumps({"x": 1}))

    import google.oauth2.credentials as _gcreds
    monkeypatch.setattr(
        _gcreds.Credentials, "from_authorized_user_info",
        classmethod(lambda cls, info, scopes=None: _FakeCreds()),
    )

    captured = {}

    import google_auth_httplib2

    class _FakeAuthorizedHttp:
        def __init__(self, creds, http=None):
            captured["http"] = http

    monkeypatch.setattr(google_auth_httplib2, "AuthorizedHttp", _FakeAuthorizedHttp)
    # build() just needs to not explode and return something inspectable.
    monkeypatch.setattr(yt, "build", lambda *a, **k: ("service", k.get("http")))

    yt.get_authenticated_service()

    http = captured.get("http")
    assert http is not None, "AuthorizedHttp never received an http instance"
    assert isinstance(http, httplib2.Http)
    assert 308 not in http.redirect_codes, (
        "308 must be stripped from redirect_codes or resumable uploads break"
    )


# ---------------------------------------------------------------------------
# Wistia ref via hex-UUID temp path (web path)
# ---------------------------------------------------------------------------
def test_build_entry_infers_wistia_ref_from_original_name_not_temp_path():
    """The web path reassembles the Shorts upload into a hex-UUID temp file
    with no date code in its name. build_entry must infer the Wistia ref from
    the ORIGINAL filename carried on MediaDateEntry.youtube_shorts_name."""
    from core.file_scanner import MediaDateEntry
    from core.session_state import SessionState

    s = SessionState()
    media = MediaDateEntry(
        date="2026-06-01",
        display_date="June 01, 2026",
        # Server temp path: opaque UUID dir + UUID filename, zero date code.
        youtube_shorts_path="/data/uploads/abcdef0123456789/0123456789abcdef",
        youtube_shorts_name="app 260601.mp4",
    )
    entry = s.build_entry("2026-06-01", media=media, meta={})
    assert entry.wistia_ref == "app 260601"


def test_build_entry_wistia_ref_falls_back_to_path_when_no_name():
    """Agent path (and older callers) don't set youtube_shorts_name; the
    local path still carries the original date code, so inference must fall
    back to the path."""
    from core.file_scanner import MediaDateEntry
    from core.session_state import SessionState

    s = SessionState()
    media = MediaDateEntry(
        date="2026-06-01",
        display_date="June 01, 2026",
        youtube_shorts_path="/Users/me/shorts/app 260601.mp4",
        # youtube_shorts_name intentionally unset (None).
    )
    entry = s.build_entry("2026-06-01", media=media, meta={})
    assert entry.wistia_ref == "app 260601"


# ---------------------------------------------------------------------------
# image_gatherer uses LLM_MODEL, not hardcoded "local"
# ---------------------------------------------------------------------------
def test_image_gatherer_uses_configured_llm_model(monkeypatch):
    """The keyword-generation POST must send the configured LLM_MODEL. The
    hosted deploy runs Ollama, which 404s on an unknown model name — the
    old hardcoded "local" produced zero terms and tanked every Rock image."""
    from core import image_gatherer
    from core import llm_title_gen

    # Force the model to a known sentinel so we can assert it propagates.
    monkeypatch.setattr(llm_title_gen, "LLM_MODEL", "llama3.2")
    monkeypatch.setattr(image_gatherer, "is_llamafile_running", lambda: True)
    # Reset the shared breaker so a prior test that tripped it can't make
    # _topic_terms_for_verse short-circuit before the POST.
    image_gatherer._LLM_KEYWORDS_BREAKER.reset()

    captured = {}

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {
                "content": '["calm water", "soft dawn", "still meadow"]'
            }}]}

    def _fake_post(url, json=None, timeout=None, **kw):
        captured["url"] = url
        captured["json"] = json
        return _FakeResp()

    monkeypatch.setattr(image_gatherer.requests, "post", _fake_post)

    terms = image_gatherer._topic_terms_for_verse("Be still and know")
    assert terms == ["calm water", "soft dawn", "still meadow"]
    assert captured["json"]["model"] == "llama3.2", (
        f"expected model=llama3.2, got {captured['json'].get('model')!r}"
    )


# ---------------------------------------------------------------------------
# Vista Social visibility — enabled platform never silently vanishes
# ---------------------------------------------------------------------------
def _vista_session(shorts_path):
    from core.file_scanner import MediaDateEntry
    from core.session_state import SessionState

    s = SessionState()
    media = MediaDateEntry(
        date="2026-06-01",
        display_date="June 01, 2026",
        youtube_shorts_path=shorts_path,
    )
    entry = s.build_entry(
        "2026-06-01", media=media, meta={},
        global_platforms={"vista_social": True},
    )
    s.entries["2026-06-01"] = entry
    s.selected_dates = ["2026-06-01"]
    return s


def test_vista_social_row_present_when_shorts_file_present():
    s = _vista_session("/data/uploads/uuid/uuid")
    rows = [r for r in s.get_summary() if r["platform"] == "Vista Social"]
    assert len(rows) == 1


def test_vista_social_row_present_even_without_shorts_file():
    """The regression: an enabled Vista Social must still appear in the
    summary when the Shorts file is missing, so the user sees a visible
    'Shorts file not found' error instead of the row silently disappearing."""
    s = _vista_session(None)
    rows = [r for r in s.get_summary() if r["platform"] == "Vista Social"]
    assert len(rows) == 1, (
        "Vista Social must not silently vanish from the summary when enabled"
    )


def test_summary_includes_all_platforms_without_server_side_files():
    """Hybrid-agent regression: the browser uploads NO files to the server on
    the agent path, so entry.youtube_video_path / youtube_shorts_path /
    podcast_path are all None. get_summary previously gated YouTube Video,
    YouTube Shorts, and SimpleCast on those paths, so the rows vanished and
    those platforms were never dispatched to the agent (only Rock/RockEmail/
    Vista survived). They must now appear from the platform flag alone, like
    Vista/Rock."""
    from core.session_state import SessionState
    s = SessionState()
    entry = s.build_entry(
        "2026-06-02", media=None, meta={},
        global_platforms={
            "youtube_video": True, "youtube_shorts": True, "simplecast": True,
            "rock": True, "rock_email": True, "vista_social": True,
        },
    )
    s.entries["2026-06-02"] = entry
    s.selected_dates = ["2026-06-02"]
    platforms = {r["platform"] for r in s.get_summary()}
    assert {"YouTube Video", "YouTube Shorts", "SimpleCast",
            "Rock", "Rock Email", "Vista Social"} <= platforms, (
        f"platforms missing from summary on the agent (no-server-file) path: {platforms}"
    )
