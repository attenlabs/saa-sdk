// SPDX-License-Identifier: Apache-2.0
//
// End-to-end contract test: instantiate the real @attenlabs/saa-js
// AttentionClient (not a mock), attach @attenlabs/saa-gate to it, fire
// the canonical event stream through the SDK's internal emitter, and
// verify the gate produces the expected allow/drop decisions.
//
// A pure unit test in gate.test.mjs covers the state machine; this
// file proves the gate's subscribe-to-handler wiring works against the
// real SDK class when fed the canonical event sequence.
//
// Scope caveat: events are fired via the SDK's internal `emit()` (see
// the helper below), which bypasses the SDK's own `turn_ready` →
// `turnReady` emit path. The test therefore catches drift in the
// GATE's subscription names but NOT drift between the SDK's emit
// names and what the gate listens to. That cross-package alignment
// is tracked separately as the upstream-coordination follow-up for
// the `speechReady` / `turnReady` rename.

import assert from "node:assert/strict";
import test from "node:test";

import { AttentionClient } from "@attenlabs/saa-js";
import { createSaaGate } from "@attenlabs/saa-gate";

/**
 * Helper to drive events through the SDK's private `emit` method.
 * `private` in TypeScript is compile-time only; the method exists at
 * runtime. This bypasses the SDK's own message-dispatch path so the
 * test stays decoupled from server-message → emit-name translation.
 */
function emit(client, event, payload) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  client["emit"](event, payload);
}

test("gate attaches to a real AttentionClient instance", () => {
  const client = new AttentionClient({ token: "test-token-not-sent" });
  const gate = createSaaGate({
    profile: "desktop",
    onAllowSpeech: () => {},
    onDropSpeech: () => {},
  });

  const detach = gate.attach(client);
  assert.equal(typeof detach, "function", "attach must return a detach fn");
  detach();
});

test("gate fires onAllowSpeech when the cloud emits an addressed turn", async () => {
  const client = new AttentionClient({ token: "test-token-not-sent" });
  const allowed = [];
  const dropped = [];
  const gate = createSaaGate({
    profile: "desktop",
    onAllowSpeech: ({ speech }) => allowed.push(speech),
    onDropSpeech: (decision) => dropped.push(decision),
  });
  const detach = gate.attach(client);

  // Canonical event stream that the cloud emits for a device-directed turn:
  // a few positive predictions, then a vad pulse, then speechReady.
  emit(client, "prediction", { cls: 2, confidence: 0.91, source: "saa-cloud", numFaces: 1 });
  emit(client, "prediction", { cls: 2, confidence: 0.88, source: "saa-cloud", numFaces: 1 });
  emit(client, "vad", { probability: 0.96, isSpeech: true });
  emit(client, "stats", { rttMs: 50, bufferedAmount: 0, sentVideo: 0, skippedVideo: 0, sentAudio: 0, uptimeMs: 0 });
  emit(client, "speechReady", {
    audioBase64: "AAAA",
    audioPcm16: new Int16Array(1600),
    durationSec: 0.8,
    frames: [],
  });

  // Allow async onAllowSpeech to settle.
  await new Promise((r) => setTimeout(r, 0));

  assert.equal(allowed.length, 1, "one allow decision per addressed speechReady");
  assert.equal(dropped.length, 0, "no drops during a clean addressed turn");
  assert.equal(allowed[0].durationSec, 0.8);

  detach();
});

test("gate drops speech that arrives during the agent's own playback", async () => {
  const client = new AttentionClient({ token: "test-token-not-sent" });
  const allowed = [];
  const dropped = [];
  const gate = createSaaGate({
    profile: "desktop",
    onAllowSpeech: ({ speech }) => allowed.push(speech),
    onDropSpeech: (decision) => dropped.push(decision),
  });
  const detach = gate.attach(client);

  // Use the gate's lifecycle wrapper so the SDK's markResponding gets
  // toggled; while the agent is "speaking", inbound speech is dropped.
  let respondingState = false;
  const origMark = client.markResponding.bind(client);
  client.markResponding = (active) => {
    respondingState = active;
    return origMark(active);
  };

  await gate.withAgentSpeech(async () => {
    assert.equal(respondingState, true, "agent-speaking lifecycle asserts markResponding(true)");
    // While the agent is speaking, an inbound speechReady event must drop.
    emit(client, "speechReady", {
      audioBase64: "AAAA",
      audioPcm16: new Int16Array(1600),
      durationSec: 0.5,
      frames: [],
    });
  });
  // Tail period blocks until echoTailMs passes; await it.
  await new Promise((r) => setTimeout(r, 500));

  assert.equal(allowed.length, 0, "no allows during agent playback");
  assert.ok(dropped.length >= 1, "the inbound speechReady was dropped");
  assert.equal(dropped[0].reason, "agent-speaking");
  assert.equal(respondingState, false, "markResponding(false) asserted after the agent finished");

  detach();
});

test("gate fails closed when transport health is unknown", async () => {
  const client = new AttentionClient({ token: "test-token-not-sent" });
  const allowed = [];
  const dropped = [];
  const gate = createSaaGate({
    profile: "desktop",
    onAllowSpeech: () => allowed.push(true),
    onDropSpeech: (d) => dropped.push(d),
  });
  const detach = gate.attach(client);

  // No stats event = transport health is unknown. With
  // failClosedWhenUnhealthy = true (default), an inbound speechReady
  // should drop.
  emit(client, "speechReady", {
    audioBase64: "AAAA",
    audioPcm16: new Int16Array(1600),
    durationSec: 0.8,
    frames: [],
  });
  await new Promise((r) => setTimeout(r, 0));

  assert.equal(allowed.length, 0);
  assert.equal(dropped.length, 1);
  assert.equal(dropped[0].reason, "transport-unhealthy");

  detach();
});
