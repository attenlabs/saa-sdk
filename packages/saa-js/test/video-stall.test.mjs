// Server video stall -> audio-only reconnect for AttentionClient.
//
// Deterministic: a fake WebSocket opens on a microtask and records its URL and
// what was sent; the server's messages and close are driven into its handlers.
// Math.random is pinned to 0 so a reconnect fires on the next timer tick.
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
    queueMicrotask(() => {
      this.readyState = FakeWebSocket.OPEN;
      this.onopen?.();
    });
  }
  send(data) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3; // fire-and-forget, as stop() uses it
  }
  // test drivers
  serverSays(obj) {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
  serverCloses(code, reason) {
    this.readyState = 3;
    this.onclose?.({ code, reason, wasClean: true });
  }
  videoFrames() {
    return this.sent.filter((d) => d instanceof ArrayBuffer && new Uint8Array(d)[0] === 0x02).length;
  }
}

const JPEG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0, 0, 0xff, 0xd9]).buffer;

function installWs() {
  const prev = { ws: globalThis.WebSocket, random: Math.random };
  globalThis.WebSocket = FakeWebSocket;
  FakeWebSocket.instances = [];
  Math.random = () => 0;
  return () => {
    globalThis.WebSocket = prev.ws;
    Math.random = prev.random;
  };
}

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

async function until(cond, what) {
  for (let i = 0; i < 200 && !cond(); i++) await tick(5);
  assert.ok(cond(), `timed out waiting for ${what}`);
}

function record(client) {
  const log = [];
  for (const name of ["error", "disconnected", "reconnecting", "reconnected"]) {
    client.on(name, (e) => log.push({ name, e }));
  }
  return log;
}

const errorsOf = (log) => log.filter((x) => x.name === "error").map((x) => x.e);
const profiles = (url) => new URL(url).searchParams.getAll("server_profile");

// feed mode: no getUserMedia, no capture pipelines
function feedClient(opts = {}) {
  return new AttentionClient({
    url: "wss://backend.example/ws",
    enableAudio: false,
    enableVideo: false,
    ...opts,
  });
}

// ── tests ───────────────────────────────────────────────────────────────────

test("a coded stall reconnects audio-only, overriding an explicit profile", async () => {
  const restore = installWs();
  const client = feedClient({
    url: "wss://backend.example/ws?server_profile=default",
    serverProfile: "default",
  });
  const log = record(client);
  try {
    await client.start();
    const ws1 = FakeWebSocket.instances[0];
    client.feedVideo(JPEG);
    assert.equal(ws1.videoFrames(), 1);

    ws1.serverSays({ type: "error", code: "video_stall", message: "Video stopped for 16 s." });
    const [err] = errorsOf(log);
    assert.equal(err.title, "Video stalled");
    assert.equal(err.kind, "video");
    assert.equal(err.retriable, true);
    assert.equal(err.detail, "Video stopped for 16 s.");
    assert.match(err.message, /Reconnecting audio-only\.$/);

    // fed frames are dropped from now on, without throwing
    client.feedVideo(JPEG);
    assert.equal(ws1.videoFrames(), 1);

    ws1.serverCloses(1011, "video stall");
    await until(() => log.some((x) => x.name === "reconnected"), "reconnected");
    assert.deepEqual(
      log.map((x) => x.name),
      ["error", "disconnected", "reconnecting", "reconnected"],
    );
    assert.deepEqual(profiles(ws1.url), ["default"]);
    assert.deepEqual(profiles(FakeWebSocket.instances[1].url), ["audio_only"]);
  } finally {
    await client.stop();
    restore();
  }
});

for (const [frames, title] of [[0, "No video received"], [2, "Video stalled"]]) {
  test(`a close-only stall after ${frames} frames reports "${title}"`, async () => {
    const restore = installWs();
    const client = feedClient({ serverProfile: "default" });
    const log = record(client);
    try {
      await client.start();
      const ws1 = FakeWebSocket.instances[0];
      for (let i = 0; i < frames; i++) client.feedVideo(JPEG);

      ws1.serverCloses(1011, "video stall");
      await until(() => FakeWebSocket.instances.length === 2, "the reconnect");
      const [err] = errorsOf(log);
      assert.equal(err.title, title);
      assert.equal(err.kind, "video");
      assert.equal(err.code, 1011);
      assert.equal(err.detail, "close code 1011 (video stall)");
      assert.deepEqual(profiles(ws1.url), []);
      assert.deepEqual(profiles(FakeWebSocket.instances[1].url), ["audio_only"]);
    } finally {
      await client.stop();
      restore();
    }
  });
}

test("any other 1011 reconnects on the same profile", async () => {
  const restore = installWs();
  const client = feedClient({ serverProfile: "default" });
  const log = record(client);
  try {
    await client.start();
    FakeWebSocket.instances[0].serverCloses(1011, "keepalive ping timeout");
    await until(() => log.some((x) => x.name === "reconnected"), "reconnected");
    assert.deepEqual(errorsOf(log), []);
    assert.equal(FakeWebSocket.instances[1].url, FakeWebSocket.instances[0].url);
  } finally {
    await client.stop();
    restore();
  }
});

test("the next start() asks for the caller's profile again", async () => {
  const restore = installWs();
  const client = feedClient({ serverProfile: "default" });
  try {
    await client.start();
    FakeWebSocket.instances[0].serverSays({ type: "error", code: "no_video", message: "m" });
    await client.stop();

    await client.start();
    const ws = FakeWebSocket.instances.at(-1);
    assert.deepEqual(profiles(ws.url), []);
    client.feedVideo(JPEG);
    assert.equal(ws.videoFrames(), 1);
  } finally {
    await client.stop();
    restore();
  }
});

test("a server error without a code is still a Server Error", async () => {
  const restore = installWs();
  const client = feedClient();
  const log = record(client);
  try {
    await client.start();
    FakeWebSocket.instances[0].serverSays({ type: "error", message: "Server full" });
    const [err] = errorsOf(log);
    assert.equal(err.title, "Server Error");
    assert.equal(err.kind, "server");
  } finally {
    await client.stop();
    restore();
  }
});

// ── own camera: the frame pipeline stops and the camera is released ───────

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

function installCamera() {
  const audio = { kind: "audio", stopped: false, stop() { this.stopped = true; } };
  const video = { kind: "video", stopped: false, stop() { this.stopped = true; } };
  const stream = {
    getTracks: () => [audio, video],
    getAudioTracks: () => [audio],
    getVideoTracks: () => [video],
  };
  const canvas = {
    width: 0,
    height: 0,
    getContext: () => ({ drawImage() {} }),
    toBlob: (cb) => cb({ arrayBuffer: async () => JPEG.slice(0) }),
  };
  const prev = {
    navigator: globalThis.navigator,
    document: globalThis.document,
    AudioContext: globalThis.AudioContext,
    AudioWorkletNode: globalThis.AudioWorkletNode,
  };
  Object.defineProperty(globalThis, "navigator", {
    value: { mediaDevices: { getUserMedia: async () => stream } },
    configurable: true,
    writable: true,
  });
  globalThis.document = {
    createElement: () => canvas,
    addEventListener() {},
    removeEventListener() {},
    visibilityState: "visible",
  };
  globalThis.AudioContext = FakeAudioContext;
  globalThis.AudioWorkletNode = FakeAudioWorkletNode;
  return {
    audio,
    video,
    restore() {
      Object.defineProperty(globalThis, "navigator", {
        value: prev.navigator,
        configurable: true,
        writable: true,
      });
      globalThis.document = prev.document;
      globalThis.AudioContext = prev.AudioContext;
      globalThis.AudioWorkletNode = prev.AudioWorkletNode;
    },
  };
}

test("with its own camera, the client stops capture and releases the camera", async () => {
  const restoreWs = installWs();
  const cam = installCamera();
  const client = new AttentionClient({
    url: "wss://backend.example/ws",
    workletUrl: "data:application/javascript,",
  });
  const log = record(client);
  const videoElement = { videoWidth: 640, videoHeight: 480, srcObject: null, addEventListener() {} };
  try {
    await client.start({ videoElement });
    const ws1 = FakeWebSocket.instances[0];
    await until(() => ws1.videoFrames() > 0, "a captured frame");

    ws1.serverSays({ type: "error", code: "video_stall", message: "m" });
    const sentBefore = ws1.videoFrames();
    assert.equal(cam.video.stopped, true);
    assert.equal(cam.audio.stopped, false);
    await tick(600); // two capture intervals
    assert.equal(ws1.videoFrames(), sentBefore);

    ws1.serverCloses(1011, "video stall");
    await until(() => log.some((x) => x.name === "reconnected"), "reconnected");
    const ws2 = FakeWebSocket.instances[1];
    assert.deepEqual(profiles(ws2.url), ["audio_only"]);
    await tick(300);
    assert.equal(ws2.videoFrames(), 0);
    assert.equal(errorsOf(log).length, 1);
  } finally {
    await client.stop();
    cam.restore();
    restoreWs();
  }
});
