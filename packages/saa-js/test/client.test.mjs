// node:test suite for @attenlabs/saa-js AttentionClient edge cases.
// Uses a fake WebSocket so tests run without a live broker or browser APIs.

import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { AttentionClient } from "../dist/index.js";

/** @type {typeof globalThis.WebSocket | undefined} */
let savedWebSocket;

/** @type {MockWebSocket | null} */
let activeSocket = null;

class MockWebSocket {
  static OPEN = 1;

  constructor(url, protocols) {
    this.url = url;
    this.protocols = protocols;
    this.readyState = MockWebSocket.OPEN;
    this.binaryType = "arraybuffer";
    activeSocket = this;
    // Defer open until the client assigns onopen/onclose handlers.
    setTimeout(() => this.onopen?.({}), 0);
  }

  send() {}

  close(code = 1000, reason = "") {
    setTimeout(
      () =>
        this.onclose?.({
          code,
          reason,
          wasClean: code === 1000,
        }),
      0,
    );
  }
}

afterEach(async () => {
  if (savedWebSocket !== undefined) {
    globalThis.WebSocket = savedWebSocket;
  } else {
    delete globalThis.WebSocket;
  }
  savedWebSocket = undefined;
  activeSocket = null;
});

function installMockWebSocket() {
  savedWebSocket = globalThis.WebSocket;
  // @ts-expect-error test double
  globalThis.WebSocket = MockWebSocket;
}

test("mid-session disconnect delivers error when prediction listener was never registered", async () => {
  installMockWebSocket();

  const errors = [];
  const client = new AttentionClient({
    token: "bad-token",
    url: "wss://test.example/ws",
    enableAudio: false,
    enableVideo: false,
  });

  client.on("error", (event) => {
    errors.push(event);
  });

  await client.start();
  assert.ok(activeSocket);
  assert.deepEqual(activeSocket.protocols, ["bad-token"]);

  activeSocket.close(1006, "connection dropped");

  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(errors.length, 1);
  assert.equal(errors[0].title, "Connection Failed");
  assert.match(errors[0].message, /Could not reach the server/);
  assert.equal(errors[0].code, 1006);

  await client.stop();
});
