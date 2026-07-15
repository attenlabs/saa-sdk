"""Guards against a stale socket's callbacks firing into a newer live session.

stop() joins the ws thread with a 2s timeout, so a slow (>2s) close handshake
lets a superseded socket's on_close/on_open/on_message land after a fresh
start() replaced self._ws. Without an identity guard those callbacks tear down
(or emit spurious events against) the new live session.

websocket-client passes the WebSocketApp as the first handler arg, so the fix
identity-checks it against the current self._ws. These tests drive the handlers
directly with two sentinel "socket" objects — no real network or threads.
"""

from saa.client import AttentionClient


class FakeWs:
    """Stand-in for a websocket.WebSocketApp — identity is all that matters."""

    def __init__(self, name):
        self.name = name

    def close(self):
        pass


def make_client():
    return AttentionClient(
        url="ws://test.local/ws",
        enable_audio=False,
        enable_video=False,
    )


def test_stale_close_does_not_tear_down_new_session():
    client = make_client()
    events = []
    client.on_disconnected(lambda e: events.append(("disconnected", e)))
    client.on_reconnecting(lambda e: events.append(("reconnecting", e)))

    a = FakeWs("A")
    b = FakeWs("B")

    # socket A opens as the current socket
    client._ws = a
    client._on_ws_open(a)
    assert client._ws_open.is_set()

    # a fresh start() replaced the socket with B (which is now live)
    client._ws = b
    client._on_ws_open(b)
    assert client._ws_open.is_set()

    # A's delayed unclean close (1006 = retriable) finally lands
    client._on_ws_close(a, 1006, "network drop")

    # B must survive untouched: still the current socket, still "open",
    # no disconnected/reconnecting events, no reconnect loop started.
    assert client._ws is b
    assert client._ws_open.is_set(), "B's open flag must survive A's stale close"
    assert not client._ws_closed.is_set(), "A's stale close must not flag closed"
    assert client._reconnecting is False
    assert events == [], f"no events from A's stale close, got {events}"


def test_stale_open_does_not_emit_connected():
    client = make_client()
    connected = []
    client.on_connected(lambda: connected.append(True))

    a = FakeWs("A")
    b = FakeWs("B")

    # B is the live socket; A's late on_open must be ignored.
    client._ws = b
    client._on_ws_open(a)
    assert connected == [], "a superseded socket must not emit connected"


def test_stale_message_is_dropped():
    client = make_client()
    states = []
    client.on_state(lambda e: states.append(e))

    a = FakeWs("A")
    b = FakeWs("B")
    client._ws = b

    # message from the current socket is handled
    client._on_ws_message(b, '{"type": "state", "state": "listening"}')
    assert len(states) == 1

    # message from a superseded socket is dropped
    client._on_ws_message(a, '{"type": "state", "state": "responding"}')
    assert len(states) == 1, "a superseded socket's frames must be dropped"


def test_failed_initial_handshake_still_surfaces_close():
    # A socket that closes before ever opening (never mid-session) must not
    # emit lifecycle/error events, but must release the handshake waiter so
    # start()'s blocking connect can raise. The guard must not change this.
    client = make_client()
    events = []
    client.on_disconnected(lambda e: events.append(("disconnected", e)))
    client.on_error(lambda e: events.append(("error", e)))

    a = FakeWs("A")
    client._ws = a  # current socket, never opened (_ws_opened_at stays 0.0)

    client._on_ws_close(a, 1006, "unreachable")

    assert events == [], "a never-opened socket emits no lifecycle/error events"
    assert client._handshake_done.is_set(), "handshake waiter must be released"
    assert not client._ws_open.is_set()
    assert client._close_info.get("code") == 1006


def test_post_stop_close_preserves_stopping_early_return():
    # After stop() nulls self._ws and leaves _stopping True, a delayed close
    # takes the existing _stopping early-return path — the guard (which only
    # fires when self._ws is a *different* live socket) must not interfere.
    client = make_client()
    events = []
    client.on_disconnected(lambda e: events.append(e))
    client.on_reconnecting(lambda e: events.append(e))

    a = FakeWs("A")
    client._ws = a
    client._on_ws_open(a)

    # simulate stop(): socket nulled, _stopping latched until next start()
    client._ws = None
    client._stopping = True

    client._on_ws_close(a, 1000, "client stop")

    assert events == [], "post-stop close stays silent (existing behavior)"
    assert client._reconnecting is False
