// Resource-lifecycle guard tests. No network, no real media, no long timers:
// every async boundary is driven by hand-rolled fakes installed on globalThis.
import { test } from "node:test";
import assert from "node:assert/strict";

import { AttentionClient } from "../dist/index.js";
import { createAudioPipeline } from "../dist/capture.js";

// ---- shared fakes -----------------------------------------------------------

function makeDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

// Drain both microtasks and the macrotask queue until `pred()` holds. A 0ms
// timer keeps us well under any real timeout while fully settling the promises
// our fakes resolve.
async function waitFor(pred, label) {
  for (let i = 0; i < 200; i++) {
    if (pred()) return;
    await new Promise((r) => setTimeout(r, 0));
  }
  throw new Error(`waitFor timed out: ${label}`);
}

class FakeMediaStreamTrack {
  constructor(kind) {
    this.kind = kind;
    this.stopped = false;
  }
  stop() {
    this.stopped = true;
  }
}

class FakeMediaStream {
  constructor(tracks) {
    this._tracks = tracks;
  }
  getTracks() {
    return this._tracks;
  }
}

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 0; // CONNECTING
    this.sent = [];
    this.closed = false;
    this.onopen = null;
    this.onclose = null;
    this.onmessage = null;
    this.onerror = null;
    FakeWebSocket.instances.push(this);
  }
  set binaryType(_v) {}
  send(data) {
    this.sent.push(data);
  }
  close(code, reason) {
    if (this.closed) return;
    this.closed = true;
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code: code ?? 1000, reason: reason ?? "", wasClean: true });
  }
  // test driver: simulate the browser completing the handshake
  fireOpen() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
}
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSED = 3;
FakeWebSocket.instances = [];

// Full success-path AudioContext so a normal start() can finish.
class FakeAudioContext {
  constructor() {
    this.state = "running";
    this.closed = false;
    this.onstatechange = null;
    this.audioWorklet = { addModule: async () => {} };
  }
  createMediaStreamSource() {
    return { connect() {}, disconnect() {} };
  }
  close() {
    this.closed = true;
    return Promise.resolve();
  }
}

class FakeAudioWorkletNode {
  constructor() {
    this.port = { onmessage: null };
    this.onprocessorerror = null;
  }
  disconnect() {}
}

// getUserMedia handed out as controllable deferreds so a test can hold start()
// mid-await and decide exactly when the mic "arrives".
const gumPending = [];
function installNavigator() {
  // node exposes a read-only `navigator` getter, so replace the property.
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    writable: true,
    value: {
      mediaDevices: {
        getUserMedia: () => {
          const d = makeDeferred();
          const stream = new FakeMediaStream([new FakeMediaStreamTrack("audio")]);
          gumPending.push({ resolve: () => d.resolve(stream), stream });
          return d.promise;
        },
      },
    },
  });
}
function resolveNextGum() {
  const next = gumPending.shift();
  next.resolve();
  return next.stream;
}

function installGlobals() {
  globalThis.WebSocket = FakeWebSocket;
  globalThis.AudioContext = FakeAudioContext;
  globalThis.AudioWorkletNode = FakeAudioWorkletNode;
  installNavigator();
}

function resetGlobals() {
  FakeWebSocket.instances.length = 0;
  gumPending.length = 0;
}

// -----------------------------------------------------------------------------

test("createAudioPipeline closes the AudioContext when addModule rejects", async () => {
  let closed = false;
  let closeAwaited = false;
  class RejectingCtx {
    constructor() {
      this.state = "running";
      this.onstatechange = null;
      this.audioWorklet = {
        addModule: async () => {
          throw new Error("CSP blocked blob: worklet");
        },
      };
    }
    createMediaStreamSource() {
      throw new Error("should not reach source creation");
    }
    close() {
      closed = true;
      // resolve on a later microtask so a non-awaited close() would be
      // observably un-flagged if the fix forgot to await it
      return Promise.resolve().then(() => {
        closeAwaited = true;
      });
    }
  }
  globalThis.AudioContext = RejectingCtx;

  const fakeStream = new FakeMediaStream([new FakeMediaStreamTrack("audio")]);
  await assert.rejects(
    // pass an explicit worklet url so no Blob/URL machinery is needed
    createAudioPipeline(fakeStream, "https://example.test/worklet.js", {}, () => {}),
    /CSP blocked blob/,
  );

  assert.equal(closed, true, "AudioContext.close() must be called on failure");
  assert.equal(closeAwaited, true, "AudioContext.close() must be awaited");
});

test("stop() during pending getUserMedia aborts start() and leaks nothing", async () => {
  installGlobals();
  resetGlobals();

  const client = new AttentionClient({
    url: "wss://fake.test/ws",
    enableVideo: false,
    enableAudio: true,
    autoReconnect: false,
  });

  const startP = client.start();
  await waitFor(() => gumPending.length === 1, "getUserMedia issued");

  // stop() lands while the mic acquisition is still in flight
  const stream = gumPending[0].stream;
  await client.stop();

  // now the mic finally "arrives" — start() must notice the interleave
  resolveNextGum();
  await assert.rejects(startP, /aborted: stop\(\) was called while starting/);

  assert.equal(stream.getTracks()[0].stopped, true, "acquired mic track stopped");
  assert.equal(FakeWebSocket.instances.length, 0, "no WebSocket ever constructed");
  assert.equal(client.isConnected, false);

  // a fresh cycle afterwards works normally, end to end
  const start2 = client.start();
  await waitFor(() => gumPending.length === 1, "second getUserMedia issued");
  const stream2 = resolveNextGum();
  await waitFor(() => FakeWebSocket.instances.length === 1, "ws constructed");
  FakeWebSocket.instances[0].fireOpen();
  await start2;

  assert.equal(client.isConnected, true, "second start connected");
  await client.stop();
  assert.equal(client.isConnected, false, "stop tore the session down");
  assert.equal(stream2.getTracks()[0].stopped, true, "second mic track stopped");
  assert.equal(FakeWebSocket.instances[0].closed, true, "second ws closed");
});

test("stop() during pending connectWS closes the socket without leaking heartbeat", async () => {
  installGlobals();
  resetGlobals();

  const client = new AttentionClient({
    url: "wss://fake.test/ws",
    enableVideo: false,
    enableAudio: true,
    autoReconnect: false,
  });

  const startP = client.start();
  await waitFor(() => gumPending.length === 1, "getUserMedia issued");
  resolveNextGum();
  // let start() advance into connectWS; the socket exists but never opens
  await waitFor(() => FakeWebSocket.instances.length === 1, "ws constructed");
  const ws = FakeWebSocket.instances[0];
  assert.equal(ws.readyState, 0, "socket still connecting (open never fired)");

  // stop() while the handshake is pending
  await client.stop();
  // stop() closes the handshake socket with a clean code, which buildCloseError
  // maps to null — start() must still reject with a real Error, never null.
  await assert.rejects(startP, (err) => err instanceof Error);

  assert.equal(ws.closed, true, "pending socket was closed, not orphaned");
  const pings = ws.sent.filter((m) => typeof m === "string" && m.includes("\"ping\""));
  assert.equal(pings.length, 0, "no heartbeat pings sent");
  assert.equal(client.isConnected, false);

  // heartbeat interval is not running — a real ping would fire within the
  // 5s cadence; give a short window and confirm silence persists
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(
    ws.sent.filter((m) => typeof m === "string" && m.includes("\"ping\"")).length,
    0,
    "no post-abort pings",
  );
});
