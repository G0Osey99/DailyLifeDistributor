"""WebSocket test client that can't strand a frame sent right after the upgrade.

``simple_websocket.Client.handshake()`` (1.1.0) does one ``sock.recv()``,
feeds the bytes to wsproto and consumes only the *first* event — the 101
accept. When the server writes its first frame immediately after the
upgrade (``/agent/ws`` sends a presence frame from ``register_browser``)
and both land in that same ``recv()``, the frame stays parsed-but-unread
inside wsproto. The reader thread then blocks in ``sock.recv()`` and only
calls ``_handle_events()`` once *more* bytes arrive, so ``receive()`` times
out no matter how long the timeout is.

Whether the two writes coalesce depends on how quickly the client thread
reaches its first ``recv()`` — rare when a test runs alone, common under
full-suite / CI load. That was the "relay sent no presence frame within
5s" flake in ``test_relay_roundtrip`` and its sibling relay e2e tests.
"""
from __future__ import annotations

import simple_websocket


class HandshakeSafeClient(simple_websocket.Client):
    """``simple_websocket.Client`` that parses frames coalesced with the 101."""

    def handshake(self):
        super().handshake()
        # The reader thread hasn't started yet (Base.__init__ starts it after
        # handshake returns), so draining wsproto here can't race it.
        self._handle_events()
        # A close coalesced with the 101 (e.g. the relay's "unauthorized"
        # error frame + close) flips connected off, which would make
        # Base.__init__ raise before the error frame could be read. Stay
        # connected; the reader thread sees EOF and marks the socket closed,
        # and receive() still returns the buffered frames first.
        self.connected = True
