import assert from "node:assert/strict";
import test from "node:test";
import {
  DECISION,
  DEFAULT_POLICY,
  PROFILES,
  REASON,
  SaaGate,
  SaaGateTimeoutError,
  createSaaGate,
  forwardSpeechReadyToOpenAIRealtime,
  pcm16ToBase64,
  positivePredictionScore,
} from "../src/index.mjs";

function speech(overrides = {}) {
  return {
    audioBase64: "AAECAw==",
    durationSec: 1.2,
    ...overrides,
  };
}

test("allows speechReady when healthy and trusted", async () => {
  const seen = [];
  const gate = createSaaGate({
    onAllowSpeech: ({ speech }) => seen.push(speech.audioBase64),
  });
  gate.setTransportHealthy(true);
  const decision = await gate.handleSpeechReady(speech());
  assert.equal(decision.action, DECISION.ALLOW);
  assert.equal(decision.reason, REASON.ADDRESSED);
  assert.deepEqual(seen, ["AAECAw=="]);
  assert.equal(gate.getSnapshot().counters.speechAllowed, 1);
});

test("drops while privacy muted", async () => {
  const gate = new SaaGate();
  gate.setTransportHealthy(true);
  gate.mute();
  const decision = await gate.handleSpeechReady(speech());
  assert.equal(decision.action, DECISION.DROP);
  assert.equal(decision.reason, REASON.MUTED);
  assert.equal(gate.getSnapshot().counters.speechDropped, 1);
});

test("drops during assistant speech and during echo tail", async () => {
  let now = 1_000;
  const gate = new SaaGate({ clock: () => now });
  gate.setTransportHealthy(true);
  gate.markResponding(true);
  assert.equal((await gate.handleSpeechReady(speech())).reason, REASON.AGENT_SPEAKING);
  gate.markResponding(false);
  assert.equal((await gate.handleSpeechReady(speech())).reason, REASON.ECHO_TAIL);
  now += gate.policy.echoTailMs + 1;
  assert.equal((await gate.handleSpeechReady(speech())).action, DECISION.ALLOW);
});

test("strict audit mode requires recent positive prediction plus VAD", async () => {
  let now = 10_000;
  const gate = new SaaGate({
    clock: () => now,
    policy: { trustSpeechReady: false, minPositiveFrames: 2, minVadFrames: 1 },
  });
  gate.setTransportHealthy(true);
  assert.equal((await gate.handleSpeechReady(speech())).reason, REASON.NO_EVIDENCE);
  gate.observeVad({ isSpeech: true, probability: 0.9 });
  gate.observePrediction({ cls: 2, confidence: 0.8 });
  gate.observePrediction({ cls: 2, confidence: 0.82 });
  assert.equal((await gate.handleSpeechReady(speech())).action, DECISION.ALLOW);
  now += gate.policy.evidenceWindowMs + 1;
  assert.equal((await gate.handleSpeechReady(speech())).reason, REASON.NO_EVIDENCE);
});

test("duration and payload guards block malformed speechReady", async () => {
  const gate = new SaaGate();
  gate.setTransportHealthy(true);
  assert.equal((await gate.handleSpeechReady({ durationSec: 1 })).reason, REASON.NO_AUDIO);
  assert.equal((await gate.handleSpeechReady(speech({ durationSec: 0.05 }))).reason, REASON.TOO_SHORT);
  assert.equal((await gate.handleSpeechReady(speech({ durationSec: 99 }))).reason, REASON.TOO_LONG);
});

test("positivePredictionScore only trusts the addressed class (cls=2)", () => {
  assert.equal(positivePredictionScore({ cls: 2, confidence: 0.91 }), 0.91);
  assert.equal(positivePredictionScore({ cls: 1, confidence: 0.99 }), 0);
  assert.equal(positivePredictionScore({ cls: 0, confidence: 0.01 }), 0);
});

test("forwardSpeechReadyToOpenAIRealtime sends append, commit, response.create", () => {
  const sent = [];
  const dc = { send: (s) => sent.push(JSON.parse(s)) };
  forwardSpeechReadyToOpenAIRealtime(dc, speech(), { eventIdPrefix: "utt-1" });
  assert.deepEqual(sent.map((e) => e.type), [
    "input_audio_buffer.append",
    "input_audio_buffer.commit",
    "response.create",
  ]);
  assert.equal(sent[0].audio, "AAECAw==");
  assert.equal(sent[2].event_id, "utt-1:response");
});

test("forwardSpeechReadyToOpenAIRealtime rejects non-pcm16 encoding", () => {
  const dc = { send: () => {} };
  assert.throws(() => forwardSpeechReadyToOpenAIRealtime(dc, speech({ encoding: "opus" })), /pcm16/i);
  assert.throws(() => forwardSpeechReadyToOpenAIRealtime(dc, speech({ sampleRate: 48000 })), /16000/);
  // pcm16 + 16kHz passes through (no throw)
  forwardSpeechReadyToOpenAIRealtime(dc, speech({ encoding: "pcm16", sampleRate: 16000 }));
});

test("attach wires an AttentionClient-like object", async () => {
  const handlers = new Map();
  const marked = [];
  const client = {
    on(name, fn) {
      handlers.set(name, fn);
      return () => handlers.delete(name);
    },
    markResponding(value) {
      marked.push(value);
    },
  };
  const gate = new SaaGate();
  const detach = gate.attach(client);
  handlers.get("connected")({});
  await handlers.get("speechReady")(speech());
  assert.equal(gate.getSnapshot().counters.speechAllowed, 1);
  gate.markResponding(true);
  gate.markResponding(false);
  assert.deepEqual(marked, [true, false]);
  detach();
  assert.equal(handlers.size, 0);
});

// ---------------------------------------------------------------------------
// Merge-blocking additions: UNHEALTHY end-to-end, decision-shape honesty,
// policy-configurable health thresholds, decision.seq + history, reset().
// ---------------------------------------------------------------------------

test("UNHEALTHY transport fail-closes speech end-to-end", async () => {
  const decisions = [];
  const gate = new SaaGate({ onDecision: (d) => decisions.push(d) });
  // Default policy starts with failClosedWhenUnhealthy: true and
  // connected: false; the very first speechReady must drop UNHEALTHY.
  const initial = await gate.handleSpeechReady(speech());
  assert.equal(initial.action, DECISION.DROP);
  assert.equal(initial.reason, REASON.UNHEALTHY);

  // Recovering connection allows.
  gate.setTransportHealthy(true);
  const recovered = await gate.handleSpeechReady(speech());
  assert.equal(recovered.action, DECISION.ALLOW);

  // Disconnection flips fail-closed again.
  gate.setTransportHealthy(false);
  const lost = await gate.handleSpeechReady(speech());
  assert.equal(lost.action, DECISION.DROP);
  assert.equal(lost.reason, REASON.UNHEALTHY);

  assert.deepEqual(
    decisions.map((d) => d.reason),
    [REASON.UNHEALTHY, REASON.ADDRESSED, REASON.UNHEALTHY],
  );
});

test("observeStats marks transport unhealthy on high RTT or backpressure", async () => {
  const gate = new SaaGate();
  gate.setTransportHealthy(true);
  gate.observeStats({ rttMs: 50, bufferedAmount: 0 });
  assert.equal((await gate.handleSpeechReady(speech())).action, DECISION.ALLOW);
  gate.observeStats({ rttMs: 5000, bufferedAmount: 0 });
  assert.equal((await gate.handleSpeechReady(speech())).reason, REASON.UNHEALTHY);
  gate.observeStats({ rttMs: 50, bufferedAmount: 5_000_000 });
  assert.equal((await gate.handleSpeechReady(speech())).reason, REASON.UNHEALTHY);
});

test("health thresholds are policy-configurable per profile", async () => {
  // A telephony-shaped profile that tolerates higher RTT.
  const gate = new SaaGate({
    policy: { unhealthyRttMs: 6000, unhealthyBufferedAmount: 10_000_000 },
  });
  gate.setTransportHealthy(true);
  gate.observeStats({ rttMs: 3000, bufferedAmount: 0 });
  assert.equal((await gate.handleSpeechReady(speech())).action, DECISION.ALLOW);
  gate.observeStats({ rttMs: 7000, bufferedAmount: 0 });
  assert.equal((await gate.handleSpeechReady(speech())).reason, REASON.UNHEALTHY);
});

test("stats older than maxStatsAgeMs count as unknown and fail-close", async () => {
  let now = 1_000_000;
  const gate = new SaaGate({ clock: () => now });
  gate.setTransportHealthy(true);
  gate.observeStats({ rttMs: 50, bufferedAmount: 0 });
  assert.equal((await gate.handleSpeechReady(speech())).action, DECISION.ALLOW);
  // Age past the staleness window without explicit disconnection.
  now += gate.policy.maxStatsAgeMs + 5_000;
  const stale = await gate.handleSpeechReady(speech());
  assert.equal(stale.action, DECISION.DROP);
  assert.equal(stale.reason, REASON.UNHEALTHY);
});

test("decision shape uses lastObservedConfidence (not confidence) and includes seq", async () => {
  const gate = new SaaGate();
  gate.setTransportHealthy(true);
  gate.observePrediction({ cls: 2, confidence: 0.83 });
  const decision = await gate.handleSpeechReady(speech());
  assert.equal(decision.lastObservedConfidence, 0.83);
  assert.equal("confidence" in decision, false, "field renamed for honesty");
  assert.equal(decision.seq, 1);
  const next = await gate.handleSpeechReady(speech());
  assert.equal(next.seq, 2);
});

test("getHistory returns recent decisions in order, capped by historyLimit", async () => {
  const gate = new SaaGate({ policy: { historyLimit: 3 } });
  gate.setTransportHealthy(true);
  for (let i = 0; i < 5; i++) await gate.handleSpeechReady(speech());
  const history = gate.getHistory();
  assert.equal(history.length, 3);
  assert.deepEqual(history.map((d) => d.seq), [3, 4, 5]);
  assert.equal(gate.getHistory(2).length, 2);
});

test("reset() clears in-flight evidence and echo-block deadline", async () => {
  let now = 5_000;
  const gate = new SaaGate({
    clock: () => now,
    policy: { trustSpeechReady: false, minPositiveFrames: 1, minVadFrames: 1 },
  });
  gate.setTransportHealthy(true);
  gate.observeVad({ isSpeech: true, probability: 0.9 });
  gate.observePrediction({ cls: 2, confidence: 0.9 });
  assert.equal((await gate.handleSpeechReady(speech())).action, DECISION.ALLOW);
  // Mid-session "tab hidden / app backgrounded": reset wipes evidence.
  gate.reset();
  assert.equal((await gate.handleSpeechReady(speech())).reason, REASON.NO_EVIDENCE);
});

test("onError fires and decision flips to drop on downstream failure", async () => {
  const errs = [];
  const gate = new SaaGate({
    onAllowSpeech: async () => { throw new Error("stt-down"); },
    onError: (e, d) => errs.push({ e: e.message, reason: d.reason }),
  });
  gate.setTransportHealthy(true);
  const decision = await gate.handleSpeechReady(speech());
  assert.equal(decision.action, DECISION.DROP);
  assert.equal(decision.reason, REASON.DOWNSTREAM_ERROR);
  assert.equal(errs.length, 1);
  assert.equal(errs[0].e, "stt-down");
});

test("downstream timeout reports DOWNSTREAM_TIMEOUT", async () => {
  const gate = new SaaGate({
    policy: { downstreamTimeoutMs: 20 },
    onAllowSpeech: () => new Promise((resolve) => setTimeout(resolve, 200)),
  });
  gate.setTransportHealthy(true);
  const decision = await gate.handleSpeechReady(speech());
  assert.equal(decision.action, DECISION.DROP);
  assert.equal(decision.reason, REASON.DOWNSTREAM_TIMEOUT);
  assert.equal(gate.getSnapshot().counters.downstreamTimeouts, 1);
});

// ──────────────────────────────────────────────────────────────────────
// Public API surface locks. These tests codify the documented contract
// so a refactor that quietly renames or removes a constant fails CI.
// ──────────────────────────────────────────────────────────────────────

test("DEFAULT_POLICY locks the safety-critical defaults (fail closed)", () => {
  assert.equal(DEFAULT_POLICY.profile, "desktop");
  assert.equal(DEFAULT_POLICY.trustSpeechReady, true);
  assert.equal(DEFAULT_POLICY.failClosedWhenMuted, true);
  assert.equal(DEFAULT_POLICY.failClosedWhenUnhealthy, true);
  assert.equal(DEFAULT_POLICY.minPositiveFrames, 2);
  assert.equal(DEFAULT_POLICY.minVadFrames, 1);
  assert.equal(DEFAULT_POLICY.openThreshold, 0.7);
  assert.throws(() => {
    DEFAULT_POLICY.failClosedWhenMuted = false;
  }, /assignment|read only|read-only/i);
});

test("PROFILES enumerates desktop, kiosk, robot, telephony", () => {
  for (const name of ["desktop", "kiosk", "robot", "telephony"]) {
    assert.ok(PROFILES[name], `PROFILES.${name} should exist`);
    assert.equal(PROFILES[name].profile, name, `PROFILES.${name}.profile self-identifies`);
  }
  assert.throws(() => {
    PROFILES.desktop = {};
  }, /assignment|read only|read-only/i);
});

test("DECISION + REASON enums cover the documented decision shape", () => {
  assert.deepEqual(
    Object.keys(DECISION).sort(),
    ["ALLOW", "DROP"],
  );
  assert.deepEqual(
    Object.keys(REASON).sort(),
    [
      "ADDRESSED",
      "AGENT_SPEAKING",
      "DOWNSTREAM_ERROR",
      "DOWNSTREAM_TIMEOUT",
      "ECHO_TAIL",
      "MUTED",
      "NO_AUDIO",
      "NO_EVIDENCE",
      "TOO_LONG",
      "TOO_SHORT",
      "UNHEALTHY",
    ],
  );
});

test("pcm16ToBase64 round-trips a known sample and rejects non-Int16Array", () => {
  // [0x0001, 0x0002, 0x0003] little-endian: 01 00 02 00 03 00 = AQACAAMA
  const out = pcm16ToBase64(new Int16Array([1, 2, 3]));
  assert.equal(out, "AQACAAMA");
  assert.throws(() => pcm16ToBase64(new Uint8Array([1, 2, 3])), /Int16Array/);
  assert.throws(() => pcm16ToBase64([1, 2, 3]), /Int16Array/);
});

test("SaaGateTimeoutError is exported, extends Error, retains its name", () => {
  const err = new SaaGateTimeoutError("timed out after 20ms");
  assert.ok(err instanceof Error);
  assert.ok(err instanceof SaaGateTimeoutError);
  assert.equal(err.name, "SaaGateTimeoutError");
  assert.match(err.message, /timed out/);
});
