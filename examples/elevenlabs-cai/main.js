// SAA × ElevenLabs Conversational AI, production-ready browser integration.
//
// Two SDKs, side-by-side:
//
//   @attenlabs/saa-js  , selective auditory attention. Streams mic + cam
//                         to the SAA cloud, returns rich prediction events
//                         (device-directed / human-directed / silent), VAD,
//                         per-utterance speechReady, face count, gaze, and
//                         reconnect telemetry.
//
//   @elevenlabs/client , the official ElevenLabs Conversational AI SDK.
//                         Owns its own mic + speaker over WebRTC (default)
//                         or WebSocket, surfaces mode / status / message
//                         callbacks, lets the agent invoke clientTools, and
//                         accepts contextual updates / user messages.
//
// The integration's value is what SAA tells the agent ABOUT the user:
//
//   1. Mic gate        , SAA's prediction.cls===2 drives setMicMuted so
//                         the agent only hears the user when SAA believes
//                         the user is talking TO the agent.
//
//   2. Self-trigger sup, onModeChange → "speaking" toggles
//                         attention.markResponding(true) so SAA stops
//                         classifying the agent's TTS as user input.
//
//   3. Context updates , speechReady (confirmed directed utterance just
//                         ended), face-count changes, and human-directed
//                         speech sustained trigger sendContextualUpdate so
//                         the agent's prompt is enriched with attentional
//                         signal.
//
//   4. Client tools    , the agent can invoke get_user_attention,
//                         get_face_count, get_last_directed_utterance to
//                         introspect the room without spending audio tokens.
//
// All four are independent: drop any one and the other three still work.
// Switch SAA_GATE_MODE on the server (mic / context / off) to dial which
// set of signals are active without touching this file.

import { AttentionClient } from "https://esm.sh/@attenlabs/saa-js@0.3.0";
import { Conversation } from "https://esm.sh/@elevenlabs/client@^1.7.0";

const $ = (id) => document.getElementById(id);
const els = {
  video: $("video"),
  saaToken: $("saaToken"),
  tokenForm: $("tokenForm"),
  textForm: $("textForm"),
  startBtn: $("startBtn"),
  stopBtn: $("stopBtn"),
  muteBtn: $("muteBtn"),
  textInput: $("textInput"),
  sendBtn: $("sendBtn"),
  thumbUp: $("thumbUp"),
  thumbDown: $("thumbDown"),
  threshold: $("threshold"),
  thresholdValue: $("thresholdValue"),
  gateMode: $("gateMode"),
  connectionType: $("connectionType"),
  authMode: $("authMode"),
  agentId: $("agentIdLabel"),
  attention: $("attentionBar"),
  cls: $("cls"),
  conf: $("conf"),
  faces: $("faces"),
  gaze: $("gaze"),
  vad: $("vad"),
  saaState: $("saaState"),
  rtt: $("rtt"),
  reconnects: $("reconnects"),
  uptime: $("uptime"),
  elStatus: $("elStatus"),
  elMode: $("elMode"),
  micPill: $("micPill"),
  log: $("log"),
};

let attention = null;
let convo = null;
let config = null;

let lastPrediction = null;
let lastFaceCount = null;
let lastSpeechReadyAt = null;
let lastSpeechReadyDuration = 0;
let lastSpeechReadyConfidence = 0;
let sustainedHumanDirectedSince = null;
let humanDirectedContextSent = false;
let micState = "muted";
let elMode = "listening";
let elStatus = "disconnected";
let canSendFeedback = false;

const HUMAN_DIRECTED_GRACE_MS = 1500;
const MIC_PILL_STATES = {
  muted: { text: "Mic muted (waiting for SAA)", state: "muted" },
  open: { text: "Mic open (SAA: device-directed)", state: "open" },
  speaking: { text: "Agent speaking", state: "speaking" },
  disabled: { text: "Mic gate disabled", state: "disabled" },
};

function log(level, msg) {
  const line = document.createElement("div");
  line.className = `l ${level}`;
  line.textContent = `${new Date().toLocaleTimeString()}  ${msg}`;
  els.log.appendChild(line);
  els.log.scrollTop = els.log.scrollHeight;
}

function setMicPill(state) {
  micState = state;
  const meta = MIC_PILL_STATES[state] ?? MIC_PILL_STATES.muted;
  els.micPill.dataset.state = meta.state;
  els.micPill.textContent = meta.text;
}

function setRunning(running) {
  els.startBtn.disabled = running;
  els.stopBtn.disabled = !running;
  els.saaToken.disabled = running;
  els.threshold.disabled = !running;
  els.muteBtn.disabled = !running;
  els.sendBtn.disabled = !running;
  els.textInput.disabled = !running;
  els.thumbUp.disabled = !running || !canSendFeedback;
  els.thumbDown.disabled = !running || !canSendFeedback;
}

// ── Boot ───────────────────────────────────────────────────────────
async function boot() {
  // Pull config from the server so secrets stay server-side and ops can
  // change transport / gate / threshold without redeploying the bundle.
  try {
    const res = await fetch("/api/conversation-config");
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      log(
        "err",
        `cannot load /api/conversation-config: ${res.status} ${body.error ?? res.statusText}`,
      );
      els.startBtn.disabled = true;
      return;
    }
    config = await res.json();
  } catch (err) {
    log("err", `cannot reach server: ${err.message}`);
    els.startBtn.disabled = true;
    return;
  }
  els.agentId.textContent = config.agentId;
  els.connectionType.textContent = config.connectionType;
  els.authMode.textContent = config.authMode;
  els.gateMode.textContent = config.saaGateMode;
  els.threshold.value = config.saaThreshold;
  els.thresholdValue.textContent = Number(config.saaThreshold).toFixed(2);
  setMicPill(config.saaGateMode === "mic" ? "muted" : "disabled");
  log(
    "info",
    `ready, agent=${config.agentId} transport=${config.connectionType} gate=${config.saaGateMode}`,
  );
}

// ── Start ──────────────────────────────────────────────────────────
async function start() {
  const saaToken = els.saaToken.value.trim();
  if (!saaToken) {
    log("err", "paste your SAA token first, https://attentionlabs.ai/dashboard");
    return;
  }
  if (!config) {
    log("err", "config not loaded");
    return;
  }
  setRunning(true);

  // 1. Start SAA before EL so the attention layer is live before the
  //    agent ever has audio to think about.
  attention = new AttentionClient({
    token: saaToken,
    initialThreshold: Number(els.threshold.value),
  });
  wireSaaListeners();
  try {
    await attention.start({ videoElement: els.video });
  } catch (err) {
    log("err", `SAA start failed: ${err.message ?? err}`);
    setRunning(false);
    attention = null;
    return;
  }

  // 2. Resolve the ElevenLabs auth blob (server-side mint for private
  //    agents; bare agentId for public agents).
  let elAuth;
  try {
    elAuth = await resolveStartAuth();
  } catch (err) {
    log("err", `EL auth mint failed: ${err.message ?? err}`);
    await attention.stop();
    attention = null;
    setRunning(false);
    return;
  }

  // 3. Start the ElevenLabs conversation. The SDK owns its own mic + speaker.
  try {
    convo = await Conversation.startSession({
      ...elAuth,
      connectionType: config.connectionType,
      onConnect: ({ conversationId }) => log("ok", `EL connected: ${conversationId}`),
      onDisconnect: (d) => log("info", `EL disconnect: ${d?.reason ?? ""}`),
      onError: (msg, ctx) =>
        log("err", `EL error: ${msg} ${ctx ? JSON.stringify(ctx).slice(0, 120) : ""}`),
      onMessage: ({ source, message }) => onElMessage(source, message),
      onStatusChange: ({ status }) => onElStatusChange(status),
      onModeChange: ({ mode }) => onElModeChange(mode),
      onCanSendFeedbackChange: ({ canSendFeedback: c }) => onCanSendFeedbackChange(c),
      onUnhandledClientToolCall: (call) =>
        log("warn", `unhandled client tool: ${call?.tool_name ?? "?"}`),
      onDebug: (d) => {
        if ((d?.type ?? "") === "unhandled")
          log("warn", `EL debug: ${JSON.stringify(d).slice(0, 160)}`);
      },
      // ── The high-value seam ───────────────────────────────────────────
      clientTools: buildClientTools(),
      dynamicVariables: {
        // Land in the agent's prompt template at runtime. Wire your own
        // user name, locale, persona, A/B bucket, right next to these.
        // https://elevenlabs.io/docs/agents-platform/customization/personalization/dynamic-variables
        user_present: "true",
        ambient_environment: "browser-desktop",
        attention_gate_mode: config.saaGateMode,
      },
    });
  } catch (err) {
    log("err", `EL startSession failed: ${err.message ?? err}`);
    await attention.stop();
    attention = null;
    setRunning(false);
    return;
  }

  // 4. In "mic" gate mode, hold the EL mic closed until SAA opens it.
  if (config.saaGateMode === "mic") {
    try {
      convo.setMicMuted(true);
      setMicPill("muted");
    } catch (_) {
      log("warn", "SDK does not expose setMicMuted; running context-only");
    }
  } else {
    setMicPill("disabled");
  }
}

async function resolveStartAuth() {
  if (config.authMode === "public") return { agentId: config.agentId };
  if (config.connectionType === "webrtc") {
    const res = await fetch("/api/conversation-token");
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error ?? `${res.status} ${res.statusText}`);
    }
    const { token } = await res.json();
    return { conversationToken: token };
  }
  const res = await fetch("/api/signed-url");
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error ?? `${res.status} ${res.statusText}`);
  }
  const { signedUrl } = await res.json();
  return { signedUrl };
}

// ── SAA → EL wiring ─────────────────────────────────────────────────────
function wireSaaListeners() {
  attention.on("connecting", () => log("info", "SAA connecting…"));
  attention.on("connected", () => log("ok", "SAA connected"));
  attention.on("started", () => log("ok", "SAA listening (model warm)"));
  attention.on("prediction", onPrediction);
  attention.on("vad", onVad);
  attention.on("state", onSaaState);
  attention.on("speechReady", onSpeechReady);
  attention.on("stats", onStats);
  attention.on("error", (e) => log("err", `SAA ${e.title}: ${e.message}`));
  attention.on("disconnected", (e) =>
    log("info", `SAA disconnected (${e.code} ${e.reason || "clean"})`),
  );
  attention.on("reconnecting", (e) =>
    log(
      "warn",
      `SAA reconnecting (attempt ${e.attempt}, in ${(e.delayMs / 1000).toFixed(1)}s)`,
    ),
  );
  attention.on("reconnected", (e) =>
    log("ok", `SAA reconnected (after ${e.reconnectCount} drops)`),
  );
  attention.on("reconnectFailed", (e) =>
    log("err", `SAA gave up reconnecting after ${e.attempts} attempts`),
  );
}

function onPrediction(p) {
  lastPrediction = p;
  const conf = Math.min(1, Math.max(0, p.confidence ?? 0));
  els.attention.style.width = `${(conf * 100).toFixed(0)}%`;
  els.attention.dataset.cls = String(p.cls);
  els.cls.textContent = ["silent", "human", "device"][p.cls] ?? "?";
  els.conf.textContent = conf.toFixed(2);
  els.faces.textContent = String(p.numFaces ?? 0);
  els.gaze.textContent =
    p.gazeOnDevice === true ? "yes" : p.gazeOnDevice === false ? "no" : "-";

  if (!convo) return;
  if (config.saaGateMode === "off") return;
  if (elMode === "speaking") return; // never toggle gate while agent is speaking

  if (lastFaceCount !== p.numFaces && config.saaGateMode !== "off") {
    sendContextualUpdate(`[attention] ${p.numFaces} face(s) visible in the room`);
    lastFaceCount = p.numFaces;
  }

  if (p.cls === 1 && p.confidence >= 0.6) {
    sustainedHumanDirectedSince ||= Date.now();
    if (
      !humanDirectedContextSent &&
      Date.now() - sustainedHumanDirectedSince > HUMAN_DIRECTED_GRACE_MS
    ) {
      sendContextualUpdate(
        "[attention] user is speaking but not addressing you, stay silent unless they look at you again",
      );
      humanDirectedContextSent = true;
    }
  } else {
    sustainedHumanDirectedSince = null;
    humanDirectedContextSent = false;
  }

  if (config.saaGateMode !== "mic") return;
  const directed = p.cls === 2 && p.confidence >= Number(els.threshold.value);
  if (directed && micState !== "open") {
    try {
      convo.setMicMuted(false);
      setMicPill("open");
    } catch (_) {
      /* setMicMuted absent on older SDKs */
    }
  }
}

function onVad(v) {
  const prob = Math.min(1, Math.max(0, v.probability ?? 0));
  els.vad.style.width = `${(prob * 100).toFixed(0)}%`;
  els.vad.dataset.speech = v.isSpeech ? "1" : "0";
}

function onSaaState(s) {
  els.saaState.textContent = s.state;
  if (
    s.state === "idle" &&
    convo &&
    elMode !== "speaking" &&
    config.saaGateMode === "mic"
  ) {
    try {
      convo.setMicMuted(true);
      setMicPill("muted");
    } catch (_) {
      /* ignore */
    }
  }
}

function onSpeechReady(e) {
  lastSpeechReadyAt = Date.now();
  lastSpeechReadyDuration = e.durationSec;
  lastSpeechReadyConfidence = lastPrediction?.confidence ?? 0;
  if (config.saaGateMode === "off") return;
  // We don't forward audio, EL heard it through its own mic. Send a
  // structured cue so the agent has a high-confidence signal that a
  // directed utterance just landed.
  sendContextualUpdate(
    `[attention] user just finished a ${e.durationSec.toFixed(2)}s directed utterance ` +
      `(confidence ${lastSpeechReadyConfidence.toFixed(2)})`,
  );
}

function onStats(s) {
  els.rtt.textContent = s.rttMs == null ? "-" : `${Math.round(s.rttMs)} ms`;
  els.uptime.textContent = `${(s.uptimeMs / 1000).toFixed(0)} s`;
  els.reconnects.textContent = String(s.reconnectCount ?? 0);
}

// ── EL → SAA wiring ─────────────────────────────────────────────────────
function onElMessage(source, message) {
  if (!message) return;
  log(
    source === "ai" ? "agent" : "you",
    `${source === "ai" ? "agent" : "you"}: ${message}`,
  );
}

function onElStatusChange(status) {
  elStatus = status;
  els.elStatus.textContent = status;
  if (status === "disconnected" || status === "disconnecting") {
    canSendFeedback = false;
    els.thumbUp.disabled = true;
    els.thumbDown.disabled = true;
  }
}

function onElModeChange(mode) {
  elMode = mode;
  els.elMode.textContent = mode;
  if (!attention) return;
  if (mode === "speaking") {
    // Suppress SAA self-trigger on the agent's own TTS without dropping
    // the mic, dropping it would defeat barge-in.
    attention.markResponding(true);
    if (config.saaGateMode === "mic") setMicPill("speaking");
  } else {
    attention.markResponding(false);
    if (config.saaGateMode === "mic") {
      try {
        convo?.setMicMuted(true);
      } catch (_) {
        /* ignore */
      }
      setMicPill("muted");
    } else if (config.saaGateMode === "context") {
      setMicPill("disabled");
    }
  }
}

function onCanSendFeedbackChange(c) {
  canSendFeedback = c;
  els.thumbUp.disabled = !c;
  els.thumbDown.disabled = !c;
}

function sendContextualUpdate(text) {
  // Hands a non-prompting cue to the agent; the model integrates it into
  // its next turn but doesn't reply by itself.
  try {
    convo?.sendContextualUpdate(text);
  } catch (err) {
    log("warn", `sendContextualUpdate failed: ${err.message ?? err}`);
  }
}

// ── Client tools the agent can call back into the browser ───────────────
function buildClientTools() {
  return {
    get_user_attention: async () => {
      const ageS = lastSpeechReadyAt
        ? (Date.now() - lastSpeechReadyAt) / 1000
        : null;
      return JSON.stringify({
        looking_at_device: lastPrediction?.gazeOnDevice ?? null,
        faces_visible: lastPrediction?.numFaces ?? 0,
        attention_score: lastPrediction?.confidence ?? 0,
        attention_class:
          ["silent", "human-directed", "device-directed"][
            lastPrediction?.cls ?? 0
          ] ?? "unknown",
        last_directed_speech_age_s: ageS,
      });
    },
    get_face_count: async () => {
      return JSON.stringify({
        faces: lastPrediction?.numFaces ?? 0,
        face_visible: lastPrediction?.faceVisible ?? null,
      });
    },
    get_last_directed_utterance: async () => {
      if (!lastSpeechReadyAt) return JSON.stringify({ available: false });
      return JSON.stringify({
        available: true,
        duration_s: Number(lastSpeechReadyDuration.toFixed(2)),
        age_s: Number(((Date.now() - lastSpeechReadyAt) / 1000).toFixed(2)),
        confidence: Number(lastSpeechReadyConfidence.toFixed(3)),
      });
    },
  };
}

// ── UI controls ──────────────────────────────────────────────────────────
els.tokenForm?.addEventListener("submit", (e) => {
  e.preventDefault();
  start();
});
els.textForm?.addEventListener("submit", (e) => {
  e.preventDefault();
  els.sendBtn.click();
});
els.video.addEventListener("loadedmetadata", (e) => {
  // Some browsers won't auto-play the SAA-bound stream even with `muted`
  // unless we kick play() explicitly once metadata lands.
  e.target.play().catch(() => {});
});

els.startBtn.addEventListener("click", start);
els.stopBtn.addEventListener("click", async () => {
  try {
    if (convo) await convo.endSession();
  } catch (err) {
    log("warn", `EL endSession: ${err.message ?? err}`);
  }
  try {
    if (attention) await attention.stop();
  } catch (err) {
    log("warn", `SAA stop: ${err.message ?? err}`);
  }
  convo = null;
  attention = null;
  lastPrediction = null;
  lastFaceCount = null;
  lastSpeechReadyAt = null;
  sustainedHumanDirectedSince = null;
  humanDirectedContextSent = false;
  els.attention.style.width = "0%";
  els.vad.style.width = "0%";
  setMicPill(config?.saaGateMode === "mic" ? "muted" : "disabled");
  setRunning(false);
  log("info", "stopped");
});

els.muteBtn.addEventListener("click", () => {
  if (!convo) return;
  const muted = els.muteBtn.dataset.muted === "1";
  if (muted) {
    try {
      convo.setMicMuted(false);
    } catch (_) {
      /* ignore */
    }
    attention?.unmute();
    els.muteBtn.dataset.muted = "0";
    els.muteBtn.textContent = "Privacy mute";
    log("info", "privacy mute lifted");
  } else {
    try {
      convo.setMicMuted(true);
    } catch (_) {
      /* ignore */
    }
    attention?.mute();
    els.muteBtn.dataset.muted = "1";
    els.muteBtn.textContent = "Privacy mute (on)";
    log("info", "privacy mute engaged");
  }
});

els.sendBtn.addEventListener("click", () => {
  const text = els.textInput.value.trim();
  if (!text || !convo) return;
  try {
    // sendUserMessage triggers the agent's turn (vs sendContextualUpdate
    // which doesn't).
    convo.sendUserMessage(text);
    log("you", `you (typed): ${text}`);
    els.textInput.value = "";
  } catch (err) {
    log("err", `sendUserMessage: ${err.message ?? err}`);
  }
});

els.thumbUp.addEventListener("click", () => {
  try {
    convo?.sendFeedback?.(true);
    log("info", "👍 feedback sent");
  } catch (err) {
    log("warn", `sendFeedback: ${err.message ?? err}`);
  }
});
els.thumbDown.addEventListener("click", () => {
  try {
    convo?.sendFeedback?.(false);
    log("info", "👎 feedback sent");
  } catch (err) {
    log("warn", `sendFeedback: ${err.message ?? err}`);
  }
});

els.threshold.addEventListener("input", () => {
  const v = Number(els.threshold.value);
  els.thresholdValue.textContent = v.toFixed(2);
  attention?.setThreshold(v);
});

window.addEventListener("beforeunload", () => {
  try {
    convo?.endSession();
  } catch (_) {}
  try {
    attention?.stop();
  } catch (_) {}
});

boot();
