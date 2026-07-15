// Camera-failure -> audio-only fallback behavior for AttentionClient.start().
//
// Deterministic: no real network/devices/timers. A fake WebSocket opens on a
// microtask; getUserMedia and the Web Audio pipeline are stubbed on globalThis.
import { test } from "node:test";
import assert from "node:assert/strict";

import { AttentionClient } from "../dist/index.js";

// ── fakes ───────────────────────────────────────────────────────────────────

class FakeWebSocket {
  static OPEN = 1;
  static instances = [];
  constructor(url, protocols) {
    this.url = url;
    this.protocols = protocols;
    this.readyState = 0;
    this.binaryType = "";
    this.bufferedAmount = 0;
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
    FakeWebSocket.instances.push(this);
    // open on the next microtask so start()'s connect promise resolves
    queueMicrotask(() => {
      this.readyState = FakeWebSocket.OPEN;
      this.onopen?.();
    });
  }
  send(data) {
    this.sent.push(data);
  }
  close(code, reason) {
    this.readyState = 3;
    this.onclose?.({ code: code ?? 1000, reason: reason ?? "", wasClean: true });
  }
}

// A camera-side getUserMedia rejection carries a DOMException-style `name`.
function cameraError(name) {
  const e = new Error(`${name} (simulated)`);
  e.name = name;
  return e;
}

function fakeAudioStream() {
  const track = { kind: "audio", stop() {} };
  return {
    getTracks: () => [track],
    getAudioTracks: () => [track],
    getVideoTracks: () => [],
  };
}

// Minimal AudioContext/AudioWorkletNode so createAudioPipeline runs without a
// browser. A workletUrl override on the client skips URL.createObjectURL.
class FakeAudioContext {
  constructor() {
    this.state = "running";
    this.onstatechange = null;
    this.audioWorklet = { addModule: async () => {} };
  }
  createMediaStreamSource() {
    return { connect() {}, disconnect() {} };
  }
  async close() {}
}
class FakeAudioWorkletNode {
  constructor() {
    this.port = { onmessage: null };
    this.onprocessorerror = null;
  }
  disconnect() {}
}

// A truthy videoElement that records whether the client ever bound a stream to
// it (it must not, once we've fallen back to audio-only).
function fakeVideoElement() {
  return {
    _srcObjectSet: false,
    videoWidth: 640,
    set srcObject(v) {
      this._srcObjectSet = true;
    },
    get srcObject() {
      return null;
    },
    addEventListener() {},
  };
}

// Install fakes; returns a restore() + a record of getUserMedia constraints.
function installEnv(getUserMedia) {
  const calls = [];
  const wrapped = (constraints) => {
    calls.push(constraints);
    return getUserMedia(constraints, calls.length);
  };
  const prevNavigator = globalThis.navigator;
  const prevWebSocket = globalThis.WebSocket;
  const prevAudioContext = globalThis.AudioContext;
  const prevWorkletNode = globalThis.AudioWorkletNode;

  Object.defineProperty(globalThis, "navigator", {
    value: { mediaDevices: { getUserMedia: wrapped } },
    configurable: true,
    writable: true,
  });
  globalThis.WebSocket = FakeWebSocket;
  globalThis.AudioContext = FakeAudioContext;
  globalThis.AudioWorkletNode = FakeAudioWorkletNode;
  FakeWebSocket.instances = [];

  return {
    calls,
    restore() {
      Object.defineProperty(globalThis, "navigator", {
        value: prevNavigator,
        configurable: true,
        writable: true,
      });
      globalThis.WebSocket = prevWebSocket;
      globalThis.AudioContext = prevAudioContext;
      globalThis.AudioWorkletNode = prevWorkletNode;
    },
  };
}

// ── tests ───────────────────────────────────────────────────────────────────

test("camera failure falls back to an audio-only session", async () => {
  // video requested -> NotReadableError (device held); audio-only -> succeeds.
  const env = installEnv((constraints) => {
    if (constraints.video) return Promise.reject(cameraError("NotReadableError"));
    return Promise.resolve(fakeAudioStream());
  });

  const client = new AttentionClient({
    url: "wss://backend.example/ws",
    workletUrl: "data:application/javascript,",
  });

  const errors = [];
  client.on("error", (e) => errors.push(e));

  const videoEl = fakeVideoElement();
  try {
    await client.start({ videoElement: videoEl });

    // start() resolved despite the camera failure
    assert.equal(client.isConnected, true);

    // an advisory environment error was emitted
    const envError = errors.find((e) => e.kind === "environment");
    assert.ok(envError, "expected an error event with kind 'environment'");
    assert.equal(envError.title, "Camera unavailable");
    assert.match(envError.detail, /NotReadableError/);

    // both getUserMedia attempts happened: video first, then audio-only
    assert.equal(env.calls.length, 2);
    assert.ok(env.calls[0].video, "first attempt requested video");
    assert.equal(env.calls[1].video, false, "retry dropped video");

    // the dialed WS URL carries the audio_only server profile
    assert.equal(FakeWebSocket.instances.length, 1);
    assert.match(FakeWebSocket.instances[0].url, /server_profile=audio_only/);

    // no frame capture: the video element was never bound to a stream
    assert.equal(videoEl._srcObjectSet, false);
  } finally {
    await client.stop();
    env.restore();
  }
});

test("a fresh start() after stop() re-attempts video", async () => {
  const env = installEnv((constraints) => {
    if (constraints.video) return Promise.reject(cameraError("NotFoundError"));
    return Promise.resolve(fakeAudioStream());
  });

  const client = new AttentionClient({
    url: "wss://backend.example/ws",
    workletUrl: "data:application/javascript,",
  });

  try {
    await client.start({ videoElement: fakeVideoElement() });
    await client.stop();

    const callsAfterFirst = env.calls.length; // 2
    assert.equal(callsAfterFirst, 2);

    await client.start({ videoElement: fakeVideoElement() });

    // the second session tried video again (wish restored, not pinned audio-only)
    assert.ok(env.calls.length > callsAfterFirst);
    assert.ok(
      env.calls[callsAfterFirst].video,
      "second start() re-requested video",
    );
  } finally {
    await client.stop();
    env.restore();
  }
});

test("when audio-only also fails, start() rejects with the audio error", async () => {
  const audioFailure = cameraError("AbortError"); // distinct audio-side failure
  const env = installEnv((constraints) => {
    if (constraints.video) return Promise.reject(cameraError("NotReadableError"));
    return Promise.reject(audioFailure);
  });

  const client = new AttentionClient({
    url: "wss://backend.example/ws",
    workletUrl: "data:application/javascript,",
  });

  try {
    await assert.rejects(
      () => client.start({ videoElement: fakeVideoElement() }),
      (err) => err === audioFailure,
    );

    // started is reset, so a subsequent start() is allowed (no "already started")
    assert.equal(client.isConnected, false);
    await assert.rejects(
      () => client.start({ videoElement: fakeVideoElement() }),
      (err) => err === audioFailure,
    );
  } finally {
    await client.stop();
    env.restore();
  }
});
