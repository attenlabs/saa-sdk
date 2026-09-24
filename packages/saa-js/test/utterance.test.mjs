// Utterance handling surface: URL/body opt-in, event dispatch, control frames.
// No network: the client's socket is replaced with a recording fake.
import { test } from "node:test";
import assert from "node:assert/strict";

import { AttentionClient } from "../dist/index.js";
import { allocateBody, applyUtteranceHandlingToWsUrl } from "../dist/url.js";

function fakeSocket() {
  const sent = [];
  return { readyState: 1, send: (s) => sent.push(JSON.parse(s)), sent };
}

function clientWithSocket(opts = {}) {
  const c = new AttentionClient({ url: "ws://x/ws", ...opts });
  const ws = fakeSocket();
  c.ws = ws; // private in TS, plain property at runtime
  return { c, ws };
}

test("allocate body carries the flag beside the profile", () => {
  assert.equal(allocateBody(undefined, true), undefined);
  assert.deepEqual(JSON.parse(allocateBody(undefined, true, true)), { utterance_handling: true });
  assert.deepEqual(JSON.parse(allocateBody("audio_only", true, true)), {
    server_profile: "audio_only",
    utterance_handling: true,
  });
  assert.deepEqual(JSON.parse(allocateBody(undefined, false)), { server_profile: "audio_only" });
});

test("direct URL gets the query param only when enabled", () => {
  assert.equal(applyUtteranceHandlingToWsUrl("ws://x/ws", false), "ws://x/ws");
  assert.equal(applyUtteranceHandlingToWsUrl("ws://x/ws", true), "ws://x/ws?utterance_handling=1");
  assert.equal(
    applyUtteranceHandlingToWsUrl("ws://x/ws?server_profile=audio_only", true),
    "ws://x/ws?server_profile=audio_only&utterance_handling=1",
  );
});

test("utterance_ended is mapped to the camelCase event with decoded audio", () => {
  const { c } = clientWithSocket({ utteranceHandling: true });
  const got = [];
  c.on("utteranceEnded", (e) => got.push(e));
  // int16 [1, -2] little-endian → base64
  const b64 = Buffer.from(new Int16Array([1, -2]).buffer).toString("base64");
  c.handleServerMessage({
    type: "utterance_ended",
    seq: 3,
    text: "What is the weather tomorrow?",
    prediction: 2,
    confidence: 0.9999,
    decision: "respond",
    reason: "scored",
    start_s: 10,
    end_s: 11.5,
    truncated: false,
    assistant_turns: 1,
    preview: true,
    latency_ms: 900,
    audio_base64: b64,
  });
  assert.equal(got.length, 1);
  const e = got[0];
  assert.deepEqual(Object.keys(e).sort(), [
    "assistantTurns", "audioBase64", "audioPcm16", "confidence", "decision", "endS", "latencyMs",
    "prediction", "preview", "reason", "seq", "startS", "text", "truncated",
  ]);
  assert.equal(e.seq, 3);
  assert.equal(e.text, "What is the weather tomorrow?");
  assert.equal(e.prediction, 2);
  assert.equal(e.confidence, 0.9999);
  assert.equal(e.decision, "respond");
  assert.equal(e.endS - e.startS, 1.5);
  assert.equal(e.truncated, false);
  assert.equal(e.assistantTurns, 1);
  assert.equal(e.preview, true);
  assert.equal(e.latencyMs, 900);
  assert.deepEqual(Array.from(e.audioPcm16), [1, -2]);
});

test("classifier_error events carry a null prediction and no audio when omitted", () => {
  const { c } = clientWithSocket();
  const got = [];
  c.on("utteranceEnded", (e) => got.push(e));
  c.handleServerMessage({
    type: "utterance_ended",
    seq: 1,
    text: "hi",
    prediction: null,
    confidence: null,
    decision: "respond",
    reason: "classifier_error",
    start_s: 0,
    end_s: 1,
    truncated: true,
    assistant_turns: 0,
    preview: false,
    latency_ms: null,
  });
  const e = got[0];
  assert.equal(e.prediction, null);
  assert.equal(e.confidence, null);
  assert.equal(e.audioBase64, null);
  assert.equal(e.audioPcm16, null);
  assert.equal(e.reason, "classifier_error");
  assert.equal(e.truncated, true);
  assert.equal(e.preview, false);
  assert.equal(e.latencyMs, null);
});

test("utterance_config is mapped, including the disabled reason", () => {
  const { c } = clientWithSocket();
  const got = [];
  c.on("utteranceConfig", (e) => got.push(e));
  c.handleServerMessage({ type: "utterance_config", enabled: true, class1_threshold: 0.9, preview: true });
  c.handleServerMessage({ type: "utterance_config", enabled: false, class1_threshold: 0.97, preview: true, reason: "no_classifier" });
  assert.deepEqual(got[0], { enabled: true, class1Threshold: 0.9, preview: true, reason: null });
  assert.deepEqual(got[1], { enabled: false, class1Threshold: 0.97, preview: true, reason: "no_classifier" });
});

test("control methods send the utterance actions", () => {
  const { c, ws } = clientWithSocket();
  assert.equal(c.addAssistantTurn("  Anything else I can help with?  "), true);
  assert.equal(c.addAssistantTurn("   "), false);
  c.setUtteranceThreshold(0.9);
  c.setUtteranceThreshold(5);
  c.setUtteranceThreshold(-1);
  c.clearUtteranceHistory();
  assert.deepEqual(ws.sent, [
    { action: "utterance_assistant_turn", text: "Anything else I can help with?" },
    { action: "utterance_set_threshold", value: 0.9 },
    { action: "utterance_set_threshold", value: 1 },
    { action: "utterance_set_threshold", value: 0.001 },
    { action: "utterance_clear_history" },
  ]);
});

test("addAssistantTurn reports false when the socket is closed", () => {
  const c = new AttentionClient({ url: "ws://x/ws" });
  assert.equal(c.addAssistantTurn("hello"), false);
});
