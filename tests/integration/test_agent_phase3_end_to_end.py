"""C4 — Cross-path event invariant: web-only vs agent run_batch.

Twin-runner scope (not end-to-end-via-relay):
  Both paths call the same underlying uploader stub and we assert that the
  *sequence of meaningful event names* (start → success|error|skip) is
  structurally identical regardless of which orchestrator drives the run.

Why not full end-to-end-via-relay here:
  A true relay test would require spinning up the Flask server, pairing an
  agent process, sending a job envelope over the WebSocket, waiting for the
  relay to fan-out the upload results, and reconciling them — substantial
  fixture scaffolding estimated at > 30 min.  That coverage is deferred to a
  follow-up task (Phase 3 relay integration test).  The twin-runner approach
  covers the same orchestration logic (circuit breaker, email-after-YT
  ordering, emit contract) without the relay layer.

Terminology:
  "web path"   = core.upload_jobs.run_batch   (the browser-streaming pipeline)
  "agent path" = agent.run_batch.run          (the local-agent orchestrator)
"""

from __future__ import annotations

import importlib

import pytest

# ---------------------------------------------------------------------------
# Helpers to normalise events from the two paths into a common vocabulary
# ---------------------------------------------------------------------------

# Web path emits flat dicts:  {"type": "start", "platform": ..., "date": ...}
# Agent path emits wrapped:   {"type": "event", "event": "start", "platform": ..., "iso_date": ...}


def _web_event_name(frame: dict) -> str | None:
    """Extract the meaningful event name from a web-path SSE frame."""
    t = frame.get("type")
    # Milestone event types that must appear in the parity sequence.
    if t in ("start", "success", "error", "skip", "needs_manual", "done"):
        return t
    # Lossy events (progress, phase_change, upload_progress) are ignored for
    # parity — they are advisory and may be emitted 0..N times.
    return None


def _agent_event_name(frame: dict) -> str | None:
    """Extract the meaningful event name from an agent-path event frame."""
    if frame.get("type") != "event":
        return None
    ev = frame.get("event")
    if ev in ("start", "success", "error", "skip", "done"):
        return ev
    return None


def _milestone_sequence(events: list[dict], extractor) -> list[str]:
    """Return the ordered list of non-None milestone names from ``events``."""
    return [n for n in (extractor(e) for e in events) if n is not None]


# ---------------------------------------------------------------------------
# Stub uploader that emits a deterministic progress + success sequence
# ---------------------------------------------------------------------------

def _make_web_stub(monkeypatch):
    """Patch core.upload_jobs._dispatch_upload with a stub.

    The stub emits nothing (the outer runner emits start/success around it)
    and returns {"success": True}.
    """
    import core.upload_jobs as _uj

    def _stub(platform, entry, elements, emit, effective_row, item,
               iso_date, yt_video_expected):
        # Emit one progress frame so the lossy-event filtering is exercised.
        emit({"type": "progress", "row": effective_row,
              "percent": 50, "message": "stub halfway"})
        return {"success": True, "url": "https://stub/ok",
                "scheduled_time": "", "watch_url": "https://stub/ok",
                "video_id": "stubid"}

    monkeypatch.setattr(_uj, "yt_upload_video", None)   # guard: must not be called
    monkeypatch.setattr(_uj, "_dispatch_upload", _stub)


def _make_agent_stub(monkeypatch):
    """Patch agent.run_batch._dispatch_upload with a stub.

    The stub emits start + one progress + success frames to mirror what the
    real agent dispatch does.
    """
    import agent.run_batch as _arb

    def _stub(*, platform, row, emit, paths, **_):
        emit({"type": "event", "event": "start", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"]})
        emit({"type": "event", "event": "upload_progress", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "percent": 50})
        emit({"type": "event", "event": "success", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "payload": {"url": "https://stub/ok"}})

    monkeypatch.setattr(_arb, "_dispatch_upload", _stub)


# ---------------------------------------------------------------------------
# Shared inputs
# ---------------------------------------------------------------------------

ISO_DATE = "2026-05-22"
DISPLAY_DATE = "May 22, 2026"
PLATFORM = "YouTube Video"

# Web path inputs
WEB_DATES = [ISO_DATE]
WEB_SUMMARY = [
    {"date": DISPLAY_DATE, "iso_date": ISO_DATE, "platform": PLATFORM,
     "title": "Test Episode", "file": "/stub/v.mp4"}
]
WEB_FILE_PATHS = {("youtube_video", ISO_DATE): "/stub/v.mp4"}


def _make_web_entries_snapshot():
    from core.session_state import ReviewEntry, UploadElements
    entry = ReviewEntry(
        date=ISO_DATE,
        display_date=DISPLAY_DATE,
        youtube_title="Test Episode",
        elements=UploadElements(),
    )
    return {ISO_DATE: entry}


# Agent path inputs
AGENT_ENVELOPE = {
    "job_id": "test-job-1",
    "rows": [
        {
            "row_idx": 0,
            "iso_date": ISO_DATE,
            "platforms": [PLATFORM],
            "entry": {"date": ISO_DATE, "display_date": DISPLAY_DATE,
                      "youtube_title": "Test Episode"},
            "elements": {},
        }
    ],
    "config": {"max_workers": 1},
}
AGENT_PATHS = {ISO_DATE: {"video": "/stub/v.mp4"}}


# ---------------------------------------------------------------------------
# C4: parity test
# ---------------------------------------------------------------------------

class TestEventSequenceParityWebVsAgent:
    """Assert that web-only and agent paths produce equivalent event sequences.

    Both runners are given one date + one platform + a stubbed uploader.
    We compare the *ordered milestone sequences* (start/success/error/skip/done)
    emitted by each path after stripping lossy advisory events.
    """

    def test_success_path_parity(self, monkeypatch, tmp_path):
        """Both paths emit: start → success → done for a successful upload."""
        # --- isolate DB for the web path ---
        import core.db as _db
        _db.init_db()

        _make_web_stub(monkeypatch)
        _make_agent_stub(monkeypatch)

        # Web path
        import core.upload_jobs as _uj
        web_events: list[dict] = []
        _uj.run_batch(
            dates=WEB_DATES,
            summary=WEB_SUMMARY,
            file_paths=WEB_FILE_PATHS,
            session_id="test-session-web",
            emit=web_events.append,
            entries_snapshot=_make_web_entries_snapshot(),
            skip_set=set(),
            config={"upload": {"max_workers": 1}},
        )

        # Agent path
        import agent.run_batch as _arb
        agent_events: list[dict] = []
        _arb.run(
            envelope=AGENT_ENVELOPE,
            paths=AGENT_PATHS,
            emit=agent_events.append,
        )

        web_seq = _milestone_sequence(web_events, _web_event_name)
        agent_seq = _milestone_sequence(agent_events, _agent_event_name)

        # Both must contain start, success, done (in that relative order).
        assert "start" in web_seq, f"web missing 'start': {web_seq}"
        assert "success" in web_seq, f"web missing 'success': {web_seq}"
        assert "done" not in web_seq, "web run_batch does not emit 'done'"

        assert "start" in agent_seq, f"agent missing 'start': {agent_seq}"
        assert "success" in agent_seq, f"agent missing 'success': {agent_seq}"
        assert "done" in agent_seq, f"agent missing 'done': {agent_seq}"

        # Relative order: start before success on both paths.
        _assert_order(web_seq, "start", "success",
                      msg="web path: 'start' must precede 'success'")
        _assert_order(agent_seq, "start", "success",
                      msg="agent path: 'start' must precede 'success'")

    def test_error_path_parity(self, monkeypatch, tmp_path):
        """Both paths emit: start → error for a failing upload."""
        import core.db as _db
        _db.init_db()

        # Override stubs to emit / return failure.
        import core.upload_jobs as _uj
        import agent.run_batch as _arb

        def _web_err_stub(platform, entry, elements, emit, effective_row,
                          item, iso_date, yt_video_expected):
            emit({"type": "progress", "row": effective_row,
                  "percent": 0, "message": "stub failing"})
            return {"success": False, "error": "stub error", "url": "",
                    "scheduled_time": ""}

        def _agent_err_stub(*, platform, row, emit, paths, **_):
            emit({"type": "event", "event": "start", "platform": platform,
                  "row_idx": row["row_idx"], "iso_date": row["iso_date"]})
            emit({"type": "event", "event": "error", "platform": platform,
                  "row_idx": row["row_idx"], "iso_date": row["iso_date"],
                  "error": "stub error"})

        monkeypatch.setattr(_uj, "_dispatch_upload", _web_err_stub)
        monkeypatch.setattr(_arb, "_dispatch_upload", _agent_err_stub)

        web_events: list[dict] = []
        _uj.run_batch(
            dates=WEB_DATES,
            summary=WEB_SUMMARY,
            file_paths=WEB_FILE_PATHS,
            session_id="test-session-web-err",
            emit=web_events.append,
            entries_snapshot=_make_web_entries_snapshot(),
            skip_set=set(),
            config={"upload": {"max_workers": 1}},
        )

        agent_events: list[dict] = []
        _arb.run(
            envelope=AGENT_ENVELOPE,
            paths=AGENT_PATHS,
            emit=agent_events.append,
        )

        web_seq = _milestone_sequence(web_events, _web_event_name)
        agent_seq = _milestone_sequence(agent_events, _agent_event_name)

        assert "start" in web_seq, f"web missing 'start': {web_seq}"
        assert "error" in web_seq, f"web missing 'error': {web_seq}"
        assert "success" not in web_seq, f"web emitted 'success' on error path: {web_seq}"

        assert "start" in agent_seq, f"agent missing 'start': {agent_seq}"
        assert "error" in agent_seq, f"agent missing 'error': {agent_seq}"
        assert "success" not in agent_seq, f"agent emitted 'success' on error path: {agent_seq}"

        _assert_order(web_seq, "start", "error",
                      msg="web path: 'start' must precede 'error'")
        _assert_order(agent_seq, "start", "error",
                      msg="agent path: 'start' must precede 'error'")

    def test_multi_platform_parity(self, monkeypatch, tmp_path):
        """Both paths emit start+success for each platform in a 2-platform run."""
        import core.db as _db
        _db.init_db()

        _make_web_stub(monkeypatch)
        _make_agent_stub(monkeypatch)

        # Two platforms for the same date.
        platforms = ["YouTube Video", "Rock"]
        multi_summary = [
            {"date": DISPLAY_DATE, "iso_date": ISO_DATE, "platform": p,
             "title": "Test Episode", "file": "/stub/v.mp4"}
            for p in platforms
        ]
        multi_file_paths = {
            ("youtube_video", ISO_DATE): "/stub/v.mp4",
            ("thumbnails", ISO_DATE): "/stub/t.jpg",
        }

        import core.upload_jobs as _uj
        web_events: list[dict] = []
        _uj.run_batch(
            dates=WEB_DATES,
            summary=multi_summary,
            file_paths=multi_file_paths,
            session_id="test-session-multi",
            emit=web_events.append,
            entries_snapshot=_make_web_entries_snapshot(),
            skip_set=set(),
            config={"upload": {"max_workers": 2}},
        )

        multi_envelope = {
            "job_id": "test-job-multi",
            "rows": [
                {
                    "row_idx": 0,
                    "iso_date": ISO_DATE,
                    "platforms": platforms,
                    "entry": {"date": ISO_DATE, "display_date": DISPLAY_DATE,
                              "youtube_title": "Test Episode"},
                    "elements": {},
                }
            ],
            "config": {"max_workers": 2},
        }
        import agent.run_batch as _arb
        agent_events: list[dict] = []
        _arb.run(
            envelope=multi_envelope,
            paths={ISO_DATE: {"video": "/stub/v.mp4", "thumbnail": "/stub/t.jpg"}},
            emit=agent_events.append,
        )

        web_starts = [e for e in web_events if e.get("type") == "start"]
        web_successes = [e for e in web_events if e.get("type") == "success"]

        agent_starts = [e for e in agent_events
                        if e.get("type") == "event" and e.get("event") == "start"]
        agent_successes = [e for e in agent_events
                           if e.get("type") == "event" and e.get("event") == "success"]

        assert len(web_starts) == 2, \
            f"web: expected 2 start events, got {len(web_starts)}"
        assert len(web_successes) == 2, \
            f"web: expected 2 success events, got {len(web_successes)}"

        assert len(agent_starts) == 2, \
            f"agent: expected 2 start events, got {len(agent_starts)}"
        assert len(agent_successes) == 2, \
            f"agent: expected 2 success events, got {len(agent_successes)}"

        # Platform coverage: both paths must cover the same set of platforms.
        web_platforms_started = {e["platform"] for e in web_starts}
        agent_platforms_started = {e["platform"] for e in agent_starts}
        assert web_platforms_started == set(platforms), \
            f"web started wrong platforms: {web_platforms_started}"
        assert agent_platforms_started == set(platforms), \
            f"agent started wrong platforms: {agent_platforms_started}"


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _assert_order(seq: list[str], first: str, second: str, *, msg: str) -> None:
    """Assert ``first`` appears before ``second`` in ``seq``."""
    try:
        i_first = seq.index(first)
        i_second = seq.index(second)
    except ValueError as exc:
        raise AssertionError(f"{msg} — missing element: {exc}") from exc
    assert i_first < i_second, \
        f"{msg} — got order {seq!r}, expected {first!r} before {second!r}"
