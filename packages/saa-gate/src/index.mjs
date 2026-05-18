// SPDX-License-Identifier: Apache-2.0
/**
 * @attenlabs/saa-gate
 *
 * Production routing policy for SAA's speechReady event.
 *
 * The important product seam is not the SAA wire protocol. The seam is
 * whether an utterance is allowed to reach STT, LLMs, tools, or TTS. This
 * module makes that seam explicit, observable, testable, and safe by
 * default.
 */

export const DEFAULT_POLICY = Object.freeze({
  profile: "desktop",

  /**
   * When true, a speechReady event may be routed without a recent positive
   * prediction. This is the correct default because speechReady is already the
   * server-side addressee gate. Set false for local replay audits where every
   * speechReady should be backed by observed predictions in the same trace.
   */
  trustSpeechReady: true,

  /** Drop all user speech while the app's privacy mute is active. */
  failClosedWhenMuted: true,

  /** Drop all user speech while SAA transport health is not good. */
  failClosedWhenUnhealthy: true,

  /** Require this many recent positive prediction frames before audit-open. */
  minPositiveFrames: 2,

  /** Require this many recent VAD speech frames before audit-open. */
  minVadFrames: 1,

  /** SAA cls=2 (device-directed) confidence needed to count as a positive prediction. */
  openThreshold: 0.7,

  /** Once audit-open, stay open until confidence falls below this threshold. */
  closeThreshold: 0.55,

  /** Recent evidence window for predictions and VAD. */
  evidenceWindowMs: 900,

  /** Hard block while assistant audio is playing. */
  blockDuringAgentSpeech: true,

  /** Continue blocking after assistant audio ends to avoid speaker echo. */
  echoTailMs: 450,

  /** Reject suspiciously long payloads before they reach expensive services. */
  maxSpeechReadyDurationSec: 20,

  /** Reject empty or extremely short utterances, often click/noise artifacts. */
  minSpeechReadyDurationSec: 0.18,

  /** Optional downstream timeout. 0 disables. */
  downstreamTimeoutMs: 30_000,

  /** Max allowed healthy-stats age. If stats are older, health is unknown. */
  maxStatsAgeMs: 30_000,

  /** RTT in ms above which transport is considered unhealthy. */
  unhealthyRttMs: 2_000,

  /** WebSocket bufferedAmount above which transport is considered unhealthy. */
  unhealthyBufferedAmount: 2_000_000,

  /** Size of the in-memory decision history retained for getHistory(). */
  historyLimit: 64,
});

export const PROFILES = Object.freeze({
  desktop: {
    profile: "desktop",
    openThreshold: 0.7,
    closeThreshold: 0.55,
    minPositiveFrames: 2,
    minVadFrames: 1,
    echoTailMs: 450,
    maxSpeechReadyDurationSec: 20,
  },
  kiosk: {
    profile: "kiosk",
    openThreshold: 0.8,
    closeThreshold: 0.65,
    minPositiveFrames: 3,
    minVadFrames: 2,
    echoTailMs: 650,
    maxSpeechReadyDurationSec: 12,
  },
  robot: {
    profile: "robot",
    openThreshold: 0.76,
    closeThreshold: 0.6,
    minPositiveFrames: 2,
    minVadFrames: 1,
    echoTailMs: 700,
    maxSpeechReadyDurationSec: 15,
  },
  telephony: {
    profile: "telephony",
    openThreshold: 0.74,
    closeThreshold: 0.58,
    minPositiveFrames: 2,
    minVadFrames: 2,
    echoTailMs: 350,
    maxSpeechReadyDurationSec: 25,
  },
});

export const DECISION = Object.freeze({
  ALLOW: "allow",
  DROP: "drop",
});

export const REASON = Object.freeze({
  ADDRESSED: "addressed",
  MUTED: "muted",
  AGENT_SPEAKING: "agent-speaking",
  ECHO_TAIL: "echo-tail",
  UNHEALTHY: "transport-unhealthy",
  NO_EVIDENCE: "no-addressed-evidence",
  TOO_SHORT: "speech-too-short",
  TOO_LONG: "speech-too-long",
  NO_AUDIO: "no-audio",
  DOWNSTREAM_TIMEOUT: "downstream-timeout",
  DOWNSTREAM_ERROR: "downstream-error",
});

const EVENT_NAMES = [
  "prediction",
  "vad",
  "speechReady",
  "stats",
  "connected",
  "reconnected",
  "disconnected",
  "error",
];

/**
 * Factory wrapper for readability.
 *
 * @param {SaaGateOptions} options
 */
export function createSaaGate(options = {}) {
  return new SaaGate(options);
}

/**
 * Production addressee gate. It is a deterministic state machine and router:
 * it never captures media and never calls SAA Cloud itself. Feed it SAA SDK
 * events, then wire allowed speech into your downstream agent.
 */
export class SaaGate {
  constructor(options = {}) {
    const profilePatch = PROFILES[options.profile ?? DEFAULT_POLICY.profile] ?? {};
    this.policy = Object.freeze({ ...DEFAULT_POLICY, ...profilePatch, ...cleanPolicy(options.policy ?? {}) });
    this.clock = typeof options.clock === "function" ? options.clock : () => Date.now();
    this.onAllowSpeech = asFunction(options.onAllowSpeech);
    this.onDropSpeech = asFunction(options.onDropSpeech);
    this.onDecision = asFunction(options.onDecision);
    this.onMetric = asFunction(options.onMetric);
    this.onError = asFunction(options.onError);

    this.state = {
      connected: false,
      unhealthy: this.policy.failClosedWhenUnhealthy,
      muted: false,
      agentSpeaking: false,
      echoBlockUntilMs: 0,
      lastStatsAtMs: 0,
      lastPredictionAtMs: 0,
      lastVadAtMs: 0,
      positiveFrames: 0,
      negativeFrames: 0,
      vadFrames: 0,
      auditOpen: false,
      lastObservedScore: null,
      lastDecision: null,
      decisionSeq: 0,
    };

    this.history = [];

    this.counters = {
      speechReadySeen: 0,
      speechAllowed: 0,
      speechDropped: 0,
      downstreamErrors: 0,
      downstreamTimeouts: 0,
      predictionPositive: 0,
      predictionNegative: 0,
      vadSpeech: 0,
      vadSilence: 0,
    };
  }

  /**
   * Attach to any AttentionClient-like object that implements on(event, fn).
   * Returns a detach function.
   */
  attach(client) {
    if (!client || typeof client.on !== "function") {
      throw new TypeError("SaaGate.attach expects an AttentionClient-like object with on(event, fn)");
    }
    const unsubs = [];
    for (const name of EVENT_NAMES) {
      const off = client.on(name, (payload) => this.handleClientEvent(name, payload, { client }));
      if (typeof off === "function") unsubs.push(off);
    }
    if (typeof client.markResponding === "function") {
      this.markResponding = (value) => {
        this.setAgentSpeaking(value);
        client.markResponding(value);
      };
    }
    if (typeof client.mute === "function" && typeof client.unmute === "function") {
      this.mute = () => {
        this.setMuted(true);
        client.mute();
      };
      this.unmute = () => {
        this.setMuted(false);
        client.unmute();
      };
    }
    return () => {
      for (const off of unsubs.splice(0)) off();
    };
  }

  handleClientEvent(type, payload, context = {}) {
    switch (type) {
      case "connected":
      case "reconnected":
        this.setTransportHealthy(true);
        break;
      case "disconnected":
      case "error":
        this.setTransportHealthy(false);
        break;
      case "stats":
        this.observeStats(payload);
        break;
      case "prediction":
        this.observePrediction(payload);
        break;
      case "vad":
        this.observeVad(payload);
        break;
      case "speechReady":
        return this.handleSpeechReady(payload, context);
      default:
        break;
    }
    return undefined;
  }

  observeStats(stats = {}) {
    const now = this.clock();
    this.state.lastStatsAtMs = now;
    const buffered = finite(stats.bufferedAmount, 0);
    const rtt = stats.rttMs == null ? null : finite(stats.rttMs, null);
    const badRtt = rtt != null && rtt > this.policy.unhealthyRttMs;
    const badBuffer = buffered > this.policy.unhealthyBufferedAmount;
    this.setTransportHealthy(!badRtt && !badBuffer);
  }

  observePrediction(prediction = {}) {
    const now = this.clock();
    this.expireEvidence(now);
    const score = positivePredictionScore(prediction);
    const positive = score >= this.policy.openThreshold;
    const closing = score < this.policy.closeThreshold;

    this.state.lastPredictionAtMs = now;
    this.state.lastObservedScore = score;

    if (positive) {
      this.counters.predictionPositive++;
      this.state.positiveFrames++;
      this.state.negativeFrames = 0;
    } else {
      this.counters.predictionNegative++;
      this.state.negativeFrames++;
      if (closing) this.state.positiveFrames = 0;
    }
    this.refreshAuditOpen(now);
  }

  observeVad(vad = {}) {
    const now = this.clock();
    this.expireEvidence(now);
    const isSpeech = !!vad.isSpeech || finite(vad.probability, 0) >= 0.5;
    this.state.lastVadAtMs = now;
    if (isSpeech) {
      this.counters.vadSpeech++;
      this.state.vadFrames++;
    } else {
      this.counters.vadSilence++;
      this.state.vadFrames = 0;
    }
    this.refreshAuditOpen(now);
  }

  setTransportHealthy(healthy) {
    this.state.connected = !!healthy;
    this.state.unhealthy = !healthy;
    this.emitMetric("transport_healthy", healthy ? 1 : 0);
  }

  setMuted(muted) {
    this.state.muted = !!muted;
    this.emitMetric("muted", this.state.muted ? 1 : 0);
  }

  setAgentSpeaking(speaking) {
    const now = this.clock();
    const wasSpeaking = this.state.agentSpeaking;
    this.state.agentSpeaking = !!speaking;
    if (wasSpeaking && !speaking) {
      this.state.echoBlockUntilMs = now + this.policy.echoTailMs;
    }
    if (speaking) {
      this.state.echoBlockUntilMs = Number.POSITIVE_INFINITY;
    }
    this.emitMetric("agent_speaking", this.state.agentSpeaking ? 1 : 0);
  }

  /**
   * Wrap assistant audio playback or TTS generation. While `fn` is in flight,
   * the gate's local state marks the agent as speaking: speechReady events
   * arriving during this window are dropped with reason "agent-speaking", and
   * an echo-tail block is applied for `policy.echoTailMs` after `fn` resolves.
   *
   * When the gate is attached to an AttentionClient via `attach(client)`, the
   * gate's `markResponding` is rebound (see `attach()`) so calling it also
   * forwards to `client.markResponding(true)` / `(false)` — the cloud SDK
   * suppresses its own predictions during TTS too. Pre-attach (or attached to
   * a client that does not expose `markResponding`), only the gate's local
   * state is updated.
   */
  async withAgentSpeech(fn) {
    if (typeof fn !== "function") throw new TypeError("withAgentSpeech expects a function");
    this.markResponding(true);
    try {
      return await fn();
    } finally {
      this.markResponding(false);
    }
  }

  markResponding(value) {
    this.setAgentSpeaking(value);
  }

  mute() {
    this.setMuted(true);
  }

  unmute() {
    this.setMuted(false);
  }

  /**
   * Reset in-flight evidence state. Useful when re-attaching after a long
   * pause (tab hidden, app backgrounded) so stale positive frames from a
   * different temporal context do not bleed into the new session.
   */
  reset() {
    this.state.positiveFrames = 0;
    this.state.negativeFrames = 0;
    this.state.vadFrames = 0;
    this.state.lastPredictionAtMs = 0;
    this.state.lastVadAtMs = 0;
    this.state.lastStatsAtMs = 0;
    this.state.echoBlockUntilMs = 0;
    this.state.auditOpen = false;
    this.state.lastObservedScore = null;
  }

  /**
   * Decide whether a speechReady event may reach STT/LLM/TTS. If allowed,
   * calls onAllowSpeech. If dropped, calls onDropSpeech. Always calls
   * onDecision with a structured record.
   */
  async handleSpeechReady(speech = {}, context = {}) {
    const now = this.clock();
    this.counters.speechReadySeen++;
    this.expireEvidence(now);
    const decision = this.decideSpeechReady(speech, { ...context, now });
    this.recordDecision(decision);
    this.onDecision?.(decision);

    if (decision.action === DECISION.DROP) {
      this.counters.speechDropped++;
      this.emitMetric("speech_dropped", 1, { reason: decision.reason });
      this.onDropSpeech?.(decision);
      return decision;
    }

    this.counters.speechAllowed++;
    this.emitMetric("speech_allowed", 1, { reason: decision.reason });
    if (!this.onAllowSpeech) return decision;

    try {
      const result = await withOptionalTimeout(
        this.onAllowSpeech({ speech, decision, gate: this, context }),
        this.policy.downstreamTimeoutMs,
      );
      return { ...decision, downstream: { ok: true, result } };
    } catch (error) {
      const timedOut = error && error.name === "SaaGateTimeoutError";
      if (timedOut) this.counters.downstreamTimeouts++;
      else this.counters.downstreamErrors++;
      const failed = {
        ...decision,
        action: DECISION.DROP,
        reason: timedOut ? REASON.DOWNSTREAM_TIMEOUT : REASON.DOWNSTREAM_ERROR,
        error: serializeError(error),
      };
      this.recordDecision(failed);
      this.onError?.(error, failed);
      this.onDecision?.(failed);
      return failed;
    }
  }

  decideSpeechReady(speech = {}, context = {}) {
    const now = context.now ?? this.clock();
    const hardBlock = this.hardBlockReason(now);
    if (hardBlock) return this.decision(DECISION.DROP, hardBlock, speech, now, context);

    const durationSec = speechDurationSec(speech);
    if (!hasAudioPayload(speech)) return this.decision(DECISION.DROP, REASON.NO_AUDIO, speech, now, context);
    if (durationSec != null && durationSec < this.policy.minSpeechReadyDurationSec) {
      return this.decision(DECISION.DROP, REASON.TOO_SHORT, speech, now, context);
    }
    if (durationSec != null && durationSec > this.policy.maxSpeechReadyDurationSec) {
      return this.decision(DECISION.DROP, REASON.TOO_LONG, speech, now, context);
    }

    if (this.policy.trustSpeechReady || this.state.auditOpen) {
      return this.decision(DECISION.ALLOW, REASON.ADDRESSED, speech, now, context);
    }

    return this.decision(DECISION.DROP, REASON.NO_EVIDENCE, speech, now, context);
  }

  hardBlockReason(now = this.clock()) {
    if (this.policy.failClosedWhenMuted && this.state.muted) return REASON.MUTED;
    if (this.policy.blockDuringAgentSpeech && this.state.agentSpeaking) return REASON.AGENT_SPEAKING;
    if (this.policy.blockDuringAgentSpeech && now < this.state.echoBlockUntilMs) return REASON.ECHO_TAIL;
    if (this.policy.failClosedWhenUnhealthy) {
      const statsStale = this.state.lastStatsAtMs > 0 && now - this.state.lastStatsAtMs > this.policy.maxStatsAgeMs;
      if (this.state.unhealthy || statsStale) return REASON.UNHEALTHY;
    }
    return null;
  }

  /**
   * Read-only view of the current policy, state, and counters. Useful for
   * health-check endpoints and live dashboards that should not mutate the
   * gate. Returned object is a shallow copy; subsequent gate updates do not
   * mutate it.
   */
  getSnapshot() {
    return {
      policy: { ...this.policy },
      state: { ...this.state },
      counters: { ...this.counters },
    };
  }

  /**
   * Return the most recent N decisions, oldest first. Useful for production
   * debugging where you want to reconstruct the gate's behaviour without
   * round-tripping through an external log writer.
   */
  getHistory(limit) {
    const n = Number.isInteger(limit) && limit > 0 ? Math.min(limit, this.history.length) : this.history.length;
    return this.history.slice(-n);
  }

  refreshAuditOpen(now = this.clock()) {
    const predictionFresh = now - this.state.lastPredictionAtMs <= this.policy.evidenceWindowMs;
    const vadFresh = this.state.lastVadAtMs === 0 || now - this.state.lastVadAtMs <= this.policy.evidenceWindowMs;
    const hasPositive = predictionFresh && this.state.positiveFrames >= this.policy.minPositiveFrames;
    const hasVad = this.policy.minVadFrames <= 0 || (vadFresh && this.state.vadFrames >= this.policy.minVadFrames);
    this.state.auditOpen = hasPositive && hasVad;
  }

  expireEvidence(now = this.clock()) {
    if (this.state.lastPredictionAtMs && now - this.state.lastPredictionAtMs > this.policy.evidenceWindowMs) {
      this.state.positiveFrames = 0;
      this.state.negativeFrames = 0;
      this.state.auditOpen = false;
    }
    if (this.state.lastVadAtMs && now - this.state.lastVadAtMs > this.policy.evidenceWindowMs) {
      this.state.vadFrames = 0;
      this.state.auditOpen = false;
    }
  }

  decision(action, reason, speech, now, context) {
    this.state.decisionSeq++;
    return {
      action,
      reason,
      ts: new Date(now).toISOString(),
      seq: this.state.decisionSeq,
      profile: this.policy.profile,
      durationSec: speechDurationSec(speech),
      // Named `lastObservedConfidence` rather than `confidence` because in
      // the default trustSpeechReady=true mode the gate routes on the
      // cloud's server-side decision; the value is the most recent
      // prediction's cls-2 score (zero when the latest prediction was a
      // non-addressed class), not a confidence derived from this specific
      // utterance.
      lastObservedConfidence: this.state.lastObservedScore,
      auditOpen: this.state.auditOpen,
      connected: this.state.connected,
      context: sanitizeContext(context),
    };
  }

  recordDecision(decision) {
    this.state.lastDecision = decision;
    this.history.push(decision);
    const cap = this.policy.historyLimit;
    if (this.history.length > cap) {
      this.history.splice(0, this.history.length - cap);
    }
  }

  emitMetric(name, value, labels = {}) {
    this.onMetric?.({ name, value, labels, ts: new Date(this.clock()).toISOString() });
  }
}

/**
 * Send a speechReady payload into an OpenAI Realtime data channel after you
 * have disabled automatic turn detection or disabled automatic responses.
 * The call sequence matches the Realtime manual audio-buffer path:
 * append audio, commit the buffer, then request a response.
 *
 * Realtime requires PCM16 mono 16 kHz audio. If the speech payload exposes
 * an encoding or sampleRate that doesn't match, throw rather than silently
 * sending the wrong shape downstream.
 */
export function forwardSpeechReadyToOpenAIRealtime(dataChannel, speech, options = {}) {
  if (!dataChannel || typeof dataChannel.send !== "function") {
    throw new TypeError("dataChannel must expose send(string)");
  }
  const audio = speech?.audioBase64;
  if (typeof audio !== "string" || audio.length === 0) {
    throw new TypeError("speech.audioBase64 is required");
  }
  const encoding = speech?.encoding ?? speech?.payload?.encoding;
  if (encoding && !/pcm16|pcm_s16le|s16le/i.test(String(encoding))) {
    throw new TypeError(`speech.encoding ${encoding} is not pcm16; Realtime requires 16-bit PCM`);
  }
  const sampleRate = finite(speech?.sampleRate ?? speech?.payload?.sampleRate, null);
  if (sampleRate != null && sampleRate !== 16000) {
    throw new TypeError(`speech.sampleRate ${sampleRate} is not 16000; Realtime requires 16 kHz`);
  }
  sendJson(dataChannel, {
    type: "input_audio_buffer.append",
    audio,
    event_id: options.eventIdPrefix ? `${options.eventIdPrefix}:append` : undefined,
  });
  sendJson(dataChannel, {
    type: "input_audio_buffer.commit",
    event_id: options.eventIdPrefix ? `${options.eventIdPrefix}:commit` : undefined,
  });
  if (options.createResponse !== false) {
    sendJson(dataChannel, {
      type: "response.create",
      event_id: options.eventIdPrefix ? `${options.eventIdPrefix}:response` : undefined,
      response: options.response,
    });
  }
}

/**
 * Convert a PCM16 `Int16Array` to a base64 string. Works in browsers (uses
 * `btoa`) and in Node (falls back to `Buffer`); useful for serialising a
 * `speechReady.audioPcm16` payload over a transport that only accepts JSON.
 *
 * @param {Int16Array} pcm16
 * @returns {string}
 */
export function pcm16ToBase64(pcm16) {
  if (!(pcm16 instanceof Int16Array)) throw new TypeError("pcm16ToBase64 expects Int16Array");
  const bytes = new Uint8Array(pcm16.buffer, pcm16.byteOffset, pcm16.byteLength);
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  if (typeof btoa === "function") return btoa(bin);
  return globalThis.Buffer.from(bytes).toString("base64");
}

/**
 * Extract the device-directed (cls=2) confidence score from a SAA SDK
 * `prediction` event. Returns 0 for any other class so callers can use the
 * result as a single "is this addressed?" scalar without inspecting the
 * class field separately.
 *
 * @param {{cls?: number|string, confidence?: number, class?: number|string, payload?: object}} [prediction]
 * @returns {number}
 */
export function positivePredictionScore(prediction = {}) {
  const cls = prediction.cls ?? prediction.class ?? prediction.payload?.cls;
  const confidence = finite(prediction.confidence ?? prediction.payload?.confidence, 0);
  // SAA class 2 is device-directed / addressed. Non-class-2 predictions are
  // intentionally scored as zero for routing. We do not infer positives from
  // a negative class' low confidence because the SDK does not expose a full
  // class probability vector in the public `prediction` event.
  return cls === 2 || cls === "2" || cls === "device" || cls === "addressed" ? confidence : 0;
}

function sendJson(target, obj) {
  const clean = {};
  for (const [k, v] of Object.entries(obj)) {
    if (v !== undefined) clean[k] = v;
  }
  target.send(JSON.stringify(clean));
}

function cleanPolicy(policy) {
  const out = {};
  for (const [k, v] of Object.entries(policy)) {
    if (v !== undefined) out[k] = v;
  }
  return out;
}

function asFunction(value) {
  return typeof value === "function" ? value : null;
}

function finite(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function speechDurationSec(speech) {
  const d = finite(speech?.durationSec ?? speech?.duration_s ?? speech?.payload?.durationSec, null);
  return d == null || d < 0 ? null : d;
}

function hasAudioPayload(speech) {
  if (!speech) return false;
  if (typeof speech.audioBase64 === "string" && speech.audioBase64.length > 0) return true;
  if (speech.audioPcm16 instanceof Int16Array && speech.audioPcm16.length > 0) return true;
  if (typeof speech.payload?.audioBase64 === "string" && speech.payload.audioBase64.length > 0) return true;
  return false;
}

function sanitizeContext(context = {}) {
  const out = {};
  if (context.sessionId) out.sessionId = context.sessionId;
  if (context.traceId) out.traceId = context.traceId;
  return out;
}

async function withOptionalTimeout(promiseLike, timeoutMs) {
  const ms = finite(timeoutMs, 0);
  if (!ms || ms <= 0) return await promiseLike;
  let timer;
  try {
    return await Promise.race([
      promiseLike,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new SaaGateTimeoutError(`downstream timed out after ${ms}ms`)), ms);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

export class SaaGateTimeoutError extends Error {
  constructor(message) {
    super(message);
    this.name = "SaaGateTimeoutError";
  }
}

function serializeError(error) {
  if (!error) return { name: "Error", message: "unknown" };
  return {
    name: error.name || "Error",
    message: String(error.message || error),
  };
}

/**
 * @typedef {object} SaaGateOptions
 * @property {keyof typeof PROFILES} [profile]
 * @property {Partial<typeof DEFAULT_POLICY>} [policy]
 * @property {() => number} [clock]
 * @property {(event: {speech: object, decision: object, gate: SaaGate, context: object}) => unknown|Promise<unknown>} [onAllowSpeech]
 * @property {(decision: object) => void} [onDropSpeech]
 * @property {(decision: object) => void} [onDecision]
 * @property {(metric: object) => void} [onMetric]
 * @property {(error: unknown, decision: object) => void} [onError]
 */
