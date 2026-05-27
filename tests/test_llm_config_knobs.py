"""The title-suggestion request honors the `llm` config knobs."""
import core.llm_title_gen as m


class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": '["Title A", "Title B"]'}}]}


def test_request_uses_config_temperature_tokens_and_timeout(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, **kw):
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(m, "is_llamafile_running", lambda: True)
    monkeypatch.setattr(m.requests, "post", fake_post)
    monkeypatch.setattr(m, "_load_config", lambda: {"llm": {
        "temperature": 0.2,
        "max_tokens": 99,
        "request_timeout_seconds": 7,
        "num_title_suggestions": 2,
    }})

    titles = m.generate_title_suggestions("a unique transcript for the knob test")

    assert titles == ["Title A", "Title B"]
    assert captured["timeout"] == 7
    assert captured["json"]["temperature"] == 0.2
    assert captured["json"]["max_tokens"] == 99


def test_defaults_apply_when_llm_config_absent(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, **kw):
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(m, "is_llamafile_running", lambda: True)
    monkeypatch.setattr(m.requests, "post", fake_post)
    monkeypatch.setattr(m, "_load_config", lambda: {})  # no llm section

    m.generate_title_suggestions("another unique transcript without config")

    assert captured["timeout"] == m._LLM_REQUEST_TIMEOUT
    assert captured["json"]["temperature"] == m._LLM_TEMPERATURE
    assert captured["json"]["max_tokens"] == m._LLM_MAX_TOKENS


def test_cold_start_retry_on_empty_first_call(monkeypatch):
    """Field report: the first 1-2 calls after a long idle returned no
    suggestions (Ollama cold-start — first generation yields malformed
    output, parse fails, ``_attempt_generation`` returns []), then
    subsequent calls worked once the model warmed up. The outer retry
    loop now calls _attempt_generation a SECOND time when the first
    returns empty, so the cold-start path succeeds transparently."""
    calls = {"n": 0}

    class _EmptyThenGoodResp:
        def __init__(self, n):
            self.n = n
        def raise_for_status(self):
            pass
        def json(self):
            calls["n"] += 1
            if calls["n"] == 1:
                # Cold-start: malformed output the parser can't recover
                return {"choices": [{"message": {"content": "not-json garbage"}}]}
            return {"choices": [{"message": {"content": '["Warm Title 1", "Warm Title 2"]'}}]}

    def fake_post(url, json=None, timeout=None, **kw):
        return _EmptyThenGoodResp(n=calls["n"])

    monkeypatch.setattr(m, "is_llamafile_running", lambda: True)
    monkeypatch.setattr(m.requests, "post", fake_post)
    monkeypatch.setattr(m, "_load_config", lambda: {})
    monkeypatch.setattr(m.time, "sleep", lambda *_a, **_kw: None)  # no real sleep

    titles = m.generate_title_suggestions("cold-start unique transcript")
    assert titles == ["Warm Title 1", "Warm Title 2"]
    assert calls["n"] == 2, "must retry once on empty result before giving up"


def test_no_retry_on_successful_first_call(monkeypatch):
    """A successful first attempt must NOT trigger the cold-start
    retry — otherwise we'd double the LLM cost on the happy path."""
    calls = {"n": 0}

    class _GoodResp:
        def raise_for_status(self):
            pass
        def json(self):
            calls["n"] += 1
            return {"choices": [{"message": {"content": '["A", "B"]'}}]}

    monkeypatch.setattr(m, "is_llamafile_running", lambda: True)
    monkeypatch.setattr(m.requests, "post", lambda *a, **kw: _GoodResp())
    monkeypatch.setattr(m, "_load_config", lambda: {})

    titles = m.generate_title_suggestions("happy-path unique transcript")
    assert titles == ["A", "B"]
    assert calls["n"] == 1, "single attempt on success"


def test_long_transcript_truncated_before_sending_to_llm(monkeypatch):
    """Long transcripts dominate latency via prefill — title generation
    only needs ~2k chars of context for theme detection. Confirm the
    prompt sent to Ollama carries the truncated version (with ellipsis)
    while the cache hash stays keyed to the full input."""
    captured = {}

    def fake_post(url, json=None, timeout=None, **kw):
        captured["prompt"] = json["messages"][0]["content"]
        return _FakeResp()

    monkeypatch.setattr(m, "is_llamafile_running", lambda: True)
    monkeypatch.setattr(m.requests, "post", fake_post)
    monkeypatch.setattr(m, "_load_config", lambda: {})

    # 5 000 chars of repeating words — well past the 2 000-char cap.
    long_transcript = ("the quick brown fox jumps over the lazy dog " * 120).strip()
    assert len(long_transcript) > m._MAX_TRANSCRIPT_CHARS

    m.generate_title_suggestions(long_transcript)

    sent_prompt = captured["prompt"]
    # The full transcript must NOT be in the prompt.
    assert long_transcript not in sent_prompt
    # The truncated marker should be present.
    assert "…" in sent_prompt, "truncated transcript must carry the ellipsis marker"
    # And the section header that anchors the transcript is still there.
    assert "Base them on this transcript:" in sent_prompt


def test_short_transcript_not_truncated(monkeypatch):
    """Transcripts under the cap pass through verbatim (no ellipsis)."""
    captured = {}

    def fake_post(url, json=None, timeout=None, **kw):
        captured["prompt"] = json["messages"][0]["content"]
        return _FakeResp()

    monkeypatch.setattr(m, "is_llamafile_running", lambda: True)
    monkeypatch.setattr(m.requests, "post", fake_post)
    monkeypatch.setattr(m, "_load_config", lambda: {})

    short = "A peaceful evening reflection on how God provides shelter in life's storms."
    m.generate_title_suggestions(short)

    sent_prompt = captured["prompt"]
    assert short in sent_prompt
    # No ellipsis from truncation — the only ellipsis-like chars would
    # be the ones in the literal example titles ("...") above. Make
    # sure our truncation marker (the unicode '…') is absent.
    assert "…" not in sent_prompt


def test_cache_keyed_to_full_transcript_not_truncated(monkeypatch):
    """Editing the transcript past the truncation cutoff still
    invalidates the cache. Without this, an operator who fixes a typo
    deep in a long transcript would get the stale (pre-fix) titles
    on the next call."""
    long_a = ("alpha beta gamma " * 200).strip()  # ~3400 chars
    long_b = long_a + " EDITED_TAIL_TYPO_FIX"     # different past the cap
    assert len(long_a) > m._MAX_TRANSCRIPT_CHARS
    h_a = m._get_transcript_hash(long_a)
    h_b = m._get_transcript_hash(long_b)
    assert h_a != h_b, (
        "cache must distinguish two transcripts that diverge after the "
        "truncation cutoff — otherwise edits past the cap return stale results"
    )


def test_both_attempts_empty_returns_empty(monkeypatch):
    """If the LLM stays broken across both attempts, return [] — the
    cold-start retry is a safety net, not infinite retry."""
    calls = {"n": 0}

    class _EmptyResp:
        def raise_for_status(self):
            pass
        def json(self):
            calls["n"] += 1
            return {"choices": [{"message": {"content": "still broken"}}]}

    monkeypatch.setattr(m, "is_llamafile_running", lambda: True)
    monkeypatch.setattr(m.requests, "post", lambda *a, **kw: _EmptyResp())
    monkeypatch.setattr(m, "_load_config", lambda: {})
    monkeypatch.setattr(m.time, "sleep", lambda *_a, **_kw: None)

    titles = m.generate_title_suggestions("persistently-broken unique transcript")
    assert titles == []
    assert calls["n"] == 2, "tried twice (cold-start retry), no more"
