// SAA + OpenAI Realtime integration.
//
// SAA handles "is this person actually talking to me?" before any tokens are
// spent. When SAA emits a `speechReady` event, we forward the PCM16 audio to
// OpenAI's Realtime API, which transcribes, reasons, optionally calls tools,
// and streams back agent audio.
//
// Three production-grade behaviours that distinguish this from a toy demo:
//
//   1. **Ephemeral tokens.** When `/session` is reachable we mint a Realtime
//      `client_secret` server-side and the OpenAI API key never leaves the
//      relay. The browser-direct path (paste an `sk-...`) is opt-in and
//      clearly marked as dev-only.
//
//   2. **Sample-rate matching.** SAA emits PCM16 @ 16 kHz; OpenAI Realtime
//      consumes PCM16 @ 24 kHz. We upsample 16→24 kHz on the way in so the
//      model isn't fed pitch-shifted audio.
//
//   3. **Output-aware attention.** We do NOT mute the microphone while the
//      agent is speaking. We wrap audible playback with beginAgentOutput() so
//      SAA suppresses self-trigger. Hard-suppressed speechReady is not a
//      barge-in signal; provider interruption/cancel events own that path.

import { AttentionClient } from "https://esm.sh/@attenlabs/saa-js@0.3.0";

// ── minimum-viable SAA integration ──────────────────────────────────────────
//
// The seven lines below are the entire SAA integration for an OpenAI
// Realtime app. The rest of this file adds UI, logging, ephemeral
// tokens, sample-rate matching, and barge-in — all nice, none required.
//
//   const saaClient = new AttentionClient({ token: SAA_TOKEN });
//   saaClient.on("speechReady", (e) => forwardToOpenAI(e.audioBase64));
//   saaClient.on("error",        (e) => console.error("saa:", e.title, e.message));
//   saaClient.on("disconnected", ()  => console.warn("saa disconnected"));
//   saaClient.on("prediction",   (e) => updateStatusUI(e.cls, e.confidence));
//   saaClient.on("state",        (e) => console.log("saa state:", e.state));
//   await saaClient.start({ videoElement });

// ── configuration ───────────────────────────────────────────────────────────

const REALTIME_URL =
  "wss://api.openai.com/v1/realtime?model=gpt-realtime";

const SAA_INPUT_RATE = 16000;
const OPENAI_RATE = 24000;

// In-browser tool catalogue. Real apps would dispatch these into your own
// app state or a server. The demo returns deterministic mock data so the
// integration story stays the focus.
const TOOL_DEFINITIONS = [
  {
    type: "function",
    name: "get_weather",
    description: "Get the current weather for a city. Use when the user asks about weather.",
    parameters: {
      type: "object",
      properties: {
        location: { type: "string", description: "City name, e.g. 'San Francisco'" },
        units: { type: "string", enum: ["celsius", "fahrenheit"], default: "fahrenheit" },
      },
      required: ["location"],
    },
  },
  {
    type: "function",
    name: "set_timer",
    description: "Set a countdown timer. Use when the user asks to set a timer or alarm.",
    parameters: {
      type: "object",
      properties: {
        duration_seconds: { type: "integer", minimum: 1, maximum: 86400 },
        label: { type: "string", description: "Optional name for the timer." },
      },
      required: ["duration_seconds"],
    },
  },
];

const SYSTEM_INSTRUCTIONS =
  "You are a helpful, concise voice assistant. Reply in one or two short " +
  "sentences. When the user asks about weather or timers, use the provided " +
  "tools instead of making up answers.";

// ── DOM helpers ──────────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);
const els = {
  video: $("video"),
  modeBrowser: $("modeBrowser"),
  modeEphemeral: $("modeEphemeral"),
  saaToken: $("saaToken"),
  openaiKey: $("openaiKey"),
  sessionUrl: $("sessionUrl"),
  startBtn: $("startBtn"),
  stopBtn: $("stopBtn"),
  log: $("log"),
  statusSas: $("statusSas"),
  statusAgent: $("statusAgent"),
  statusGate: $("statusGate"),
  toolsList: $("toolsList"),
};

function log(msg, kind = "") {
  const line = document.createElement("div");
  line.className = "l " + kind;
  line.textContent = msg;
  els.log.appendChild(line);
  els.log.scrollTop = els.log.scrollHeight;
}

function setRunning(running) {
  els.startBtn.disabled = running;
  els.stopBtn.disabled = !running;
  for (const id of ["saaToken", "openaiKey", "sessionUrl", "modeBrowser", "modeEphemeral"]) {
    if (els[id]) els[id].disabled = running;
  }
}

function setBadge(el, label, kind = "") {
  if (!el) return;
  el.textContent = label;
  el.dataset.kind = kind;
}

function renderTools() {
  if (!els.toolsList) return;
  els.toolsList.innerHTML = "";
  for (const t of TOOL_DEFINITIONS) {
    const li = document.createElement("li");
    li.innerHTML = `<code>${t.name}</code> &mdash; ${t.description}`;
    els.toolsList.appendChild(li);
  }
}
renderTools();

// ── audio helpers ────────────────────────────────────────────────────────────

function base64ToInt16(b64) {
  const bin = atob(b64);
  const out = new Int16Array(bin.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = bin.charCodeAt(i * 2) | (bin.charCodeAt(i * 2 + 1) << 8);
    if (out[i] >= 0x8000) out[i] -= 0x10000;
  }
  return out;
}

function int16ToBase64(arr) {
  const u8 = new Uint8Array(arr.length * 2);
  for (let i = 0; i < arr.length; i++) {
    const v = arr[i] < 0 ? arr[i] + 0x10000 : arr[i];
    u8[i * 2] = v & 0xff;
    u8[i * 2 + 1] = (v >> 8) & 0xff;
  }
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < u8.length; i += chunk) {
    bin += String.fromCharCode.apply(null, u8.subarray(i, i + chunk));
  }
  return btoa(bin);
}

// Linear-interpolation resampler. Adequate for voice, OpenAI Realtime
// expects PCM16 @ 24 kHz; SAA emits PCM16 @ 16 kHz. Without this the model
// hears 1.5x-faster, pitch-shifted audio and STT degrades noticeably.
function resampleInt16(input, fromRate, toRate) {
  if (fromRate === toRate) return input;
  const ratio = toRate / fromRate;
  const outLen = Math.round(input.length * ratio);
  const out = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const srcIdx = i / ratio;
    const i0 = Math.floor(srcIdx);
    const i1 = Math.min(i0 + 1, input.length - 1);
    const frac = srcIdx - i0;
    const sample = input[i0] * (1 - frac) + input[i1] * frac;
    out[i] = Math.max(-32768, Math.min(32767, sample | 0));
  }
  return out;
}

// ── playback queue ───────────────────────────────────────────────────────────

let audioCtx = null;
let playbackQueue = [];
let playbackSource = null;
let playing = false;
let agentSpeaking = false;
let agentOutputHandle = null;

function beginAgentOutputOnce() {
  if (agentOutputHandle || !saaClient) return;
  if (typeof saaClient.beginAgentOutput === "function") {
    agentOutputHandle = saaClient.beginAgentOutput({
      source: "agent_tts",
      transport: "openai_realtime",
      mode: "suppress_speechready",
      tailMs: 250,
      hardTimeoutMs: 10_000,
      bargeInPolicy: "external_interrupt_only",
    });
  } else {
    // Compatibility with older @attenlabs/saa-js builds.
    saaClient.markResponding?.(true);
  }
}

function stopAgentOutput(reason = "drained") {
  if (agentOutputHandle) {
    agentOutputHandle.stop(reason);
    agentOutputHandle = null;
  } else {
    saaClient?.markResponding?.(false);
  }
}

function ensureAudioCtx() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: OPENAI_RATE, // OpenAI Realtime audio is 24 kHz
    });
  }
  return audioCtx;
}

function enqueuePlayback(b64) {
  const ctx = ensureAudioCtx();
  const i16 = base64ToInt16(b64);
  const f32 = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 0x8000;
  const buffer = ctx.createBuffer(1, f32.length, OPENAI_RATE);
  buffer.copyToChannel(f32, 0);
  playbackQueue.push(buffer);
  if (!playing) drainPlayback();
}

function drainPlayback() {
  const ctx = ensureAudioCtx();
  if (!playbackQueue.length) {
    playing = false;
    playbackSource = null;
    if (agentSpeaking) {
      agentSpeaking = false;
      stopAgentOutput("drained");
      setBadge(els.statusAgent, "listening", "ok");
    }
    return;
  }
  playing = true;
  const buf = playbackQueue.shift();
  const src = ctx.createBufferSource();
  src.buffer = buf;
  src.connect(ctx.destination);
  src.onended = () => {
    if (playbackSource === src) playbackSource = null;
    drainPlayback();
  };
  src.start();
  playbackSource = src;
}

function stopPlayback() {
  playbackQueue = [];
  if (playbackSource) {
    try { playbackSource.stop(); } catch {}
    try { playbackSource.disconnect(); } catch {}
    playbackSource = null;
  }
  playing = false;
  if (agentSpeaking || agentOutputHandle) {
    agentSpeaking = false;
    stopAgentOutput("interrupted");
  }
}

// ── OpenAI Realtime ──────────────────────────────────────────────────────────

let saaClient = null;
let oaWs = null;
let currentResponseId = null;
let currentAssistantTranscript = "";
let pendingFunctionCalls = new Map();

async function mintEphemeralKey(sessionEndpoint) {
  const res = await fetch(sessionEndpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "gpt-realtime",
      voice: "alloy",
      instructions: SYSTEM_INSTRUCTIONS,
      tools: TOOL_DEFINITIONS,
    }),
  });
  if (!res.ok) {
    throw new Error(`POST ${sessionEndpoint} failed: ${res.status} ${res.statusText}`);
  }
  const body = await res.json();
  const value = body?.client_secret?.value;
  if (!value) {
    throw new Error(`session response missing client_secret.value: ${JSON.stringify(body)}`);
  }
  return value;
}

async function startOpenAI(apiKey) {
  return new Promise((resolve, reject) => {
    // Realtime expects the API key as a Bearer token. Browser WebSocket can't
    // set custom headers; OpenAI accepts the key via subprotocol negotiation.
    // For ephemeral mode we pass the client_secret instead, same shape.
    const ws = new WebSocket(REALTIME_URL, [
      "realtime",
      `openai-insecure-api-key.${apiKey}`,
      "openai-beta.realtime-v1",
    ]);

    ws.onopen = () => {
      log("openai realtime: connected", "ok");
      ws.send(JSON.stringify({
        type: "session.update",
        session: {
          modalities: ["text", "audio"],
          instructions: SYSTEM_INSTRUCTIONS,
          voice: "alloy",
          input_audio_format: "pcm16",   // 24 kHz mono, see resampling on input
          output_audio_format: "pcm16",  // 24 kHz mono, drives our AudioContext
          turn_detection: null,          // SAA handles segmentation server-side
          tools: TOOL_DEFINITIONS,
          tool_choice: "auto",
          input_audio_transcription: { model: "whisper-1" },
        },
      }));
      resolve(ws);
    };

    ws.onerror = () => log("openai realtime: error", "err");
    ws.onclose = (e) => {
      log(`openai realtime: closed code=${e.code}${e.reason ? " " + e.reason : ""}`,
        e.wasClean ? "" : "err");
      if (!e.wasClean && oaWs === ws) {
        oaWs = null;
        setBadge(els.statusAgent, "disconnected", "err");
      }
    };
    ws.onmessage = (e) => {
      try { handleOpenAIMessage(JSON.parse(e.data)); }
      catch (err) { log(`openai parse error: ${err}`, "err"); }
    };
  });
}

function handleOpenAIMessage(msg) {
  switch (msg.type) {
    case "session.created":
    case "session.updated":
      setBadge(els.statusAgent, "ready", "ok");
      break;

    case "response.created":
      currentResponseId = msg.response?.id ?? null;
      currentAssistantTranscript = "";
      setBadge(els.statusAgent, "thinking");
      break;

    case "response.audio.delta":
      if (!agentSpeaking) {
        agentSpeaking = true;
        setBadge(els.statusAgent, "speaking", "agent");
        // Server-side SAA output lifecycle: don't classify our own playback as input.
        beginAgentOutputOnce();
      }
      enqueuePlayback(msg.delta);
      break;

    case "response.audio_transcript.delta":
      currentAssistantTranscript += msg.delta || "";
      break;

    case "response.audio_transcript.done":
      if (currentAssistantTranscript) {
        log(`assistant: ${currentAssistantTranscript}`, "gpt");
      }
      currentAssistantTranscript = "";
      break;

    case "conversation.item.input_audio_transcription.completed":
      if (msg.transcript) log(`you: ${msg.transcript}`, "you");
      break;

    case "response.function_call_arguments.delta":
      // Accumulate streamed args by call_id so we can dispatch on `.done`.
      {
        const call = pendingFunctionCalls.get(msg.call_id) || { args: "", name: "" };
        call.args += msg.delta || "";
        pendingFunctionCalls.set(msg.call_id, call);
      }
      break;

    case "response.function_call_arguments.done":
      handleFunctionCall(msg);
      break;

    case "response.done":
      // The model can ship several outputs in one response (audio + a tool
      // call, multiple tool calls). When response.done arrives, all the
      // streamed text/audio for this response is in.
      currentResponseId = null;
      // The response is complete, but local playback may still be draining.
      // Stop the output lifecycle only when the playback queue is actually empty.
      setTimeout(() => {
        if (!playing && playbackQueue.length === 0) {
          agentSpeaking = false;
          stopAgentOutput("drained");
          setBadge(els.statusAgent, "listening", "ok");
        }
      }, 200);
      break;

    case "input_audio_buffer.speech_started":
      // Useful diagnostic; SAA already gated by the time we get here.
      break;

    case "error":
      log(`openai error: ${msg.error?.message || JSON.stringify(msg.error)}`, "err");
      break;

    default:
      // Forward-compat: log unknown types but don't fail the loop.
      break;
  }
}

async function handleFunctionCall(doneMsg) {
  const callId = doneMsg.call_id;
  const name = doneMsg.name;
  const argsRaw = doneMsg.arguments || pendingFunctionCalls.get(callId)?.args || "{}";
  pendingFunctionCalls.delete(callId);

  let args = {};
  try { args = JSON.parse(argsRaw); } catch {}

  log(`tool call: ${name}(${JSON.stringify(args)})`, "tool");
  const result = await runTool(name, args);
  log(`tool result: ${JSON.stringify(result)}`, "tool");

  if (oaWs?.readyState !== WebSocket.OPEN) return;

  oaWs.send(JSON.stringify({
    type: "conversation.item.create",
    item: {
      type: "function_call_output",
      call_id: callId,
      output: JSON.stringify(result),
    },
  }));
  // Ask the model to incorporate the tool result into a spoken response.
  oaWs.send(JSON.stringify({ type: "response.create" }));
}

// Mock tool implementations. Replace these with calls into your app.
async function runTool(name, args) {
  switch (name) {
    case "get_weather": {
      const loc = args.location || "unknown";
      const units = args.units || "fahrenheit";
      // Deterministic stub so the demo behaves the same way every time.
      const tempF = 60 + (hashString(loc) % 40);
      const tempC = Math.round((tempF - 32) * 5 / 9);
      const conditions = ["sunny", "cloudy", "rain", "clear", "windy"][hashString(loc) % 5];
      return {
        location: loc,
        temperature: units === "celsius" ? tempC : tempF,
        units,
        conditions,
        as_of: new Date().toISOString(),
      };
    }
    case "set_timer": {
      const sec = Math.max(1, Math.floor(args.duration_seconds || 60));
      const label = args.label || "timer";
      setTimeout(() => log(`⏰ ${label} fired (${sec}s)`, "tool"), sec * 1000);
      return { ok: true, label, fires_in_seconds: sec };
    }
    default:
      return { error: `unknown tool: ${name}` };
  }
}

function hashString(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

// ── start / stop ─────────────────────────────────────────────────────────────

function selectedMode() {
  return els.modeEphemeral?.checked ? "ephemeral" : "browser";
}

async function resolveOpenAIKey() {
  const mode = selectedMode();
  if (mode === "ephemeral") {
    const url = els.sessionUrl.value.trim() || "/session";
    log(`minting ephemeral OpenAI key from ${url}`);
    return await mintEphemeralKey(url);
  }
  const k = els.openaiKey.value.trim();
  if (!k) throw new Error("paste an OpenAI API key (browser-direct mode)");
  return k;
}

function forwardSpeechReady(event) {
  if (!oaWs || oaWs.readyState !== WebSocket.OPEN) return;

  // Barge-in: if the agent was mid-response, cancel it and drop queued audio.
  if (agentSpeaking || currentResponseId) {
    log("barge-in: cancelling current response", "saa");
    oaWs.send(JSON.stringify({ type: "response.cancel" }));
    stopPlayback();
    agentSpeaking = false;
  }

  // SAA emits PCM16 @ 16 kHz; OpenAI Realtime expects PCM16 @ 24 kHz. We
  // pass through `audioBase64` only if the rates match, otherwise we
  // resample from the typed array and re-encode. The cost is one short
  // pass per utterance (~0.5–3 ms in modern browsers).
  let payload;
  if (SAA_INPUT_RATE === OPENAI_RATE) {
    payload = event.audioBase64;
  } else {
    const upsampled = resampleInt16(event.audioPcm16, SAA_INPUT_RATE, OPENAI_RATE);
    payload = int16ToBase64(upsampled);
  }

  log(`saa: utterance ${event.durationSec.toFixed(2)}s → openai`, "saa");
  oaWs.send(JSON.stringify({
    type: "input_audio_buffer.append",
    audio: payload,
  }));
  oaWs.send(JSON.stringify({ type: "input_audio_buffer.commit" }));
  oaWs.send(JSON.stringify({ type: "response.create" }));
  setBadge(els.statusGate, "forwarded", "ok");
}

els.startBtn.addEventListener("click", async () => {
  const saaTok = els.saaToken.value.trim();
  if (!saaTok) {
    log("paste a SAA token first", "err");
    return;
  }

  setRunning(true);
  ensureAudioCtx();
  setBadge(els.statusSas, "connecting");
  setBadge(els.statusAgent, "connecting");
  setBadge(els.statusGate, "idle");

  let openaiKey;
  try {
    openaiKey = await resolveOpenAIKey();
  } catch (err) {
    log(`auth failed: ${err.message ?? err}`, "err");
    setRunning(false);
    return;
  }

  try {
    oaWs = await startOpenAI(openaiKey);
  } catch (err) {
    log(`failed to start openai: ${err.message ?? err}`, "err");
    setRunning(false);
    return;
  }

  saaClient = new AttentionClient({ token: saaTok });

  saaClient.on("connected", () => {
    log("saa: connected", "ok");
    setBadge(els.statusSas, "connected", "ok");
  });
  saaClient.on("started", () => {
    log("saa: model warm, listening");
    setBadge(els.statusSas, "listening", "ok");
  });
  saaClient.on("prediction", (e) => {
    // Live class indicator. cls 0=silent, 1=human-directed, 2=device-directed.
    const labels = ["silent", "human", "device"];
    if (typeof e.cls === "number") {
      setBadge(els.statusGate, `${labels[e.cls] ?? e.cls} ${(e.confidence ?? 0).toFixed(2)}`,
        e.cls === 2 ? "ok" : "");
    }
  });
  saaClient.on("state", (e) => log(`saa state: ${e.state}`, "saa"));
  saaClient.on("speechReady", forwardSpeechReady);
  saaClient.on("error", (e) => log(`saa: ${e.title}: ${e.message}`, "err"));
  saaClient.on("disconnected", () => {
    log("saa: disconnected", "err");
    setBadge(els.statusSas, "disconnected", "err");
  });

  try {
    await saaClient.start({ videoElement: els.video });
  } catch (err) {
    log(`saa start failed: ${err?.message ?? err}`, "err");
    setRunning(false);
  }
});

els.stopBtn.addEventListener("click", async () => {
  stopPlayback();
  if (saaClient) {
    try { await saaClient.stop(); } catch {}
  }
  if (oaWs && oaWs.readyState === WebSocket.OPEN) {
    try { oaWs.close(1000, "client stop"); } catch {}
  }
  saaClient = null;
  oaWs = null;
  agentSpeaking = false;
  agentOutputHandle = null;
  pendingFunctionCalls.clear();
  currentResponseId = null;
  setRunning(false);
  setBadge(els.statusSas, "idle");
  setBadge(els.statusAgent, "idle");
  setBadge(els.statusGate, "idle");
  log("stopped");
});

// Mode toggle: hide irrelevant inputs.
function syncModeUI() {
  const ephemeral = selectedMode() === "ephemeral";
  if (els.openaiKey) els.openaiKey.parentElement.style.display = ephemeral ? "none" : "";
  if (els.sessionUrl) els.sessionUrl.parentElement.style.display = ephemeral ? "" : "none";
}
els.modeBrowser?.addEventListener("change", syncModeUI);
els.modeEphemeral?.addEventListener("change", syncModeUI);
syncModeUI();
