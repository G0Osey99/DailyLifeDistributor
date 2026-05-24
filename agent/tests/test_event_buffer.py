"""Tests for EventBuffer — bounded in-memory buffer + replay on reconnect."""
from agent.dispatch import EventBuffer


def test_appends_when_connected_and_passes_through():
    sent = []
    buf = EventBuffer(max_size=4, send=sent.append)
    buf.set_connected(True)
    buf.emit({"type": "event", "event": "start"})
    assert sent == [{"type": "event", "event": "start"}]


def test_buffers_on_disconnect_and_replays_on_reconnect():
    sent = []
    buf = EventBuffer(max_size=4, send=sent.append)
    buf.set_connected(True)
    buf.set_connected(False)
    buf.emit({"type": "event", "event": "a"})
    buf.emit({"type": "event", "event": "b"})
    assert sent == []
    buf.set_connected(True)
    assert [f["event"] for f in sent] == ["a", "b"]


def test_buffer_drops_oldest_when_full():
    sent = []
    buf = EventBuffer(max_size=2, send=sent.append)
    buf.set_connected(False)
    for i in range(5):
        buf.emit({"type": "event", "event": f"e{i}"})
    buf.set_connected(True)
    assert [f["event"] for f in sent] == ["e3", "e4"]


def test_starts_disconnected_by_default():
    """Default state is disconnected — nothing sent until set_connected(True)."""
    sent = []
    buf = EventBuffer(max_size=4, send=sent.append)
    buf.emit({"type": "event", "event": "x"})
    assert sent == []
    buf.set_connected(True)
    assert len(sent) == 1


def test_reconnect_multiple_times_replays_only_buffered():
    """Each reconnect flushes the buffer; next disconnect buffers fresh."""
    sent = []
    buf = EventBuffer(max_size=4, send=sent.append)
    buf.set_connected(True)
    buf.set_connected(False)
    buf.emit({"type": "event", "event": "a"})
    buf.set_connected(True)
    assert [f["event"] for f in sent] == ["a"]
    # Connected — emits directly now.
    buf.emit({"type": "event", "event": "b"})
    assert [f["event"] for f in sent] == ["a", "b"]
