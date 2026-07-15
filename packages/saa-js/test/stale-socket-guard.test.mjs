// Guards against a stale socket's onclose tearing down a newer live session.
//
// stop() fire-and-forgets ws.close() and nulls the socket; an immediate start()
// opens a fresh one. The old socket's onclose then lands asynchronously and, if
// unguarded, runs stopHeartbeat() / this.ws=null / emit("disconnected") /
// scheduleReconnect against the NEW live session. These tests drive hand-rolled
// fake sockets by calling their handlers directly (no real network/timers).

import { test } from "node:test";
import assert from "node:assert/strict";

import { AttentionClient } from "../dist/index.js";

// ── minimal WebSocket fake ────────────────────────────────────────────
// Models the fire-and-forget nature of ws.close(): close() does NOT fire
// onclose; the test fires it explicitly to simulate the async close event.
class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  constructor(url, protocols) {
    this.url = url;
    this.protocols = protocols;
    this.readyState = FakeWebSocket.CONNECTING;
    this.binaryType = "blob";
    this.bufferedAmount = 0;
    this.sent = [];
    this.closeCalls = [];
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
    FakeWebSocket.instances.push(this);
  }

  send(data) {
    this.sent.push(data);
  }

  close(code, reason) {
    this.closeCalls.push({ code, reason });
    this.readyState = FakeWebSocket.CLOSED;
    // deliberately does not fire onclose — the real socket delivers it later
  }

  // test drivers
  fireOpen() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  fireClose(code, reason, wasClean) {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code, reason, wasClean: wasClean ?? code === 1000 });
  }
}
FakeWebSocket.instances = [];

function installFakeWs() {
  const prev = globalThis.WebSocket;
  globalThis.WebSocket = FakeWebSocket;
  FakeWebSocket.instances = [];
  return () => {
    globalThis.WebSocket = prev;
  };
}

// connectWS() creates the socket after `await resolveWsUrl()`, so it appears a
// few microtasks after start() is called — poll for it rather than assume sync.
async function waitForNewSocket(count) {
  for (let i = 0; i < 100 && FakeWebSocket.instances.length <= count; i++) {
    await Promise.resolve();
  }
  return FakeWebSocket.instances.at(-1);
}

// audio/video disabled => no getUserMedia, no AudioContext, no videoElement.
function makeClient() {
  return new AttentionClient({
    url: "ws://test.local/ws",
    enableAudio: false,
    enableVideo: false,
  });
}

// Bring a client up on a fresh fake socket and resolve start().
async function bringUp(client) {
  const before = FakeWebSocket.instances.length;
  const p = client.start();
  const sock = await waitForNewSocket(before);
  sock.fireOpen();
  await p;
  return sock;
}

test("stale socket close does not kill a newer live session", async () => {
  const restore = installFakeWs();
  try {
    const client = makeClient();
    const events = [];
    client.on("disconnected", (e) => events.push(["disconnected", e]));
    client.on("reconnecting", (e) => events.push(["reconnecting", e]));

    // start() -> socket A
    const a = await bringUp(client);

    // stop() fire-and-forgets a.close() and nulls the client's socket
    await client.stop();
    assert.equal(a.closeCalls.length, 1, "stop() should close socket A");
    assert.equal(client.isConnected, false, "client is down after stop()");

    // start() again -> socket B open + heartbeat running
    const b = await bringUp(client);
    assert.notEqual(a, b, "second start uses a fresh socket");
    assert.equal(client.isConnected, true, "socket B is live");

    // NOW A's onclose finally lands (the delayed clean close from stop())
    a.fireClose(1000, "client stop");

    // B must survive: still connected, no "disconnected" for A, no reconnect.
    assert.equal(client.isConnected, true, "B still live after A's stale close");
    assert.equal(b.readyState, FakeWebSocket.OPEN, "B socket untouched");
    assert.equal(b.closeCalls.length, 0, "B was not closed by A's stale close");
    assert.equal(
      events.length,
      0,
      `no lifecycle events from A's stale close, got ${JSON.stringify(events)}`,
    );

    await client.stop();
  } finally {
    restore();
  }
});

test("failed initial handshake still rejects start()", async () => {
  const restore = installFakeWs();
  try {
    const client = makeClient();
    const before = FakeWebSocket.instances.length;
    const p = client.start();
    const sock = await waitForNewSocket(before);

    // socket fails before ever opening -> unclean close on a fresh handshake
    sock.fireClose(1006, "unreachable", false);

    // start() rejects with the structured close error (not an Error instance)
    await assert.rejects(p, (err) => {
      assert.equal(err.code, 1006);
      assert.equal(err.kind, "transport");
      return true;
    });
    assert.equal(client.isConnected, false);
  } finally {
    restore();
  }
});

test("post-stop close still emits disconnected (this.ws already null)", async () => {
  const restore = installFakeWs();
  try {
    const client = makeClient();
    const events = [];
    client.on("disconnected", (e) => events.push(e));

    const a = await bringUp(client);
    await client.stop(); // nulls the client's socket, but A's onclose is pending

    // the delayed close arrives after stop() already nulled this.ws
    a.fireClose(1000, "client stop");

    assert.equal(events.length, 1, "post-stop close still emits disconnected");
    assert.equal(events[0].code, 1000);
    assert.equal(events[0].wasClean, true);
  } finally {
    restore();
  }
});
