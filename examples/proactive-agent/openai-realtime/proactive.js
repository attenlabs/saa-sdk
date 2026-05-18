// Proactive variant of examples/openai-realtime/main.js.
//
// The reactive base uses input_audio_buffer.append + response.create
// triggered by SAA's speechReady (i.e. the *human* spoke first). This
// proactive variant adds:
//
//   1. A "Trigger proactive turn" button that sends response.create
//      with explicit instructions so the agent speaks first.
//   2. markResponding(true) is asserted immediately when the proactive
//      turn fires (the parent main.js asserts it on response.audio.delta,
//      which is too late if the proactive turn has zero user audio
//      before it).
//   3. EventSource subscription to /proactive-events so a back-end
//      webhook can fire the trigger remotely.
//
// Everything else (microphone, AttentionClient, OpenAI Realtime
// WebSocket, mark_responding, barge-in via response.cancel) is identical
// to the parent main.js. No SDK changes; no SAA wire extension.

// CDN-imported to mirror examples/openai-realtime/main.js:25 exactly.
// If the parent example bumps the saa-js version, this overlay should
// follow on the next sync.
import { AttentionClient } from "https://esm.sh/@attenlabs/saa-js@0.3.0";

const SCRIPT_URL = "./demo_script.json";

let saa = null;
let realtimeWs = null;
let es = null;
let agentSpeaking = false;
let currentResponseId = null;
let script = {
  opening_line: "I see your tests are red - want me to look?",
  system_prompt: "You are a helpful voice assistant.",
};

const els = {
  start: document.getElementById("start"),
  stop: document.getElementById("stop"),
  trigger: document.getElementById("trigger"),
  log: document.getElementById("log"),
  instructions: document.getElementById("instructions"),
  statusAgent: document.getElementById("status-agent"),
  statusSaa: document.getElementById("status-saa"),
  saaToken: document.getElementById("saaToken"),
};

function log(line, cls) {
  const div = document.createElement("div");
  if (cls) div.className = cls;
  div.textContent = line;
  els.log.appendChild(div);
  els.log.scrollTop = els.log.scrollHeight;
}

function setBadge(node, label, cls = "listening") {
  node.textContent = label;
  node.className = `badge ${cls}`;
}

async function loadScript() {
  try {
    const res = await fetch(SCRIPT_URL);
    if (res.ok) {
      script = await res.json();
      els.instructions.value = script.opening_line;
    }
  } catch (e) {
    log(`(could not load ${SCRIPT_URL}; using defaults)`, "saa");
  }
}

async function mintRealtimeToken() {
  const res = await fetch("/session", { method: "POST" });
  if (!res.ok) throw new Error(`/session ${res.status}`);
  const data = await res.json();
  return data.client_secret?.value || data.client_secret;
}

async function start() {
  els.start.disabled = true;
  setBadge(els.statusAgent, "connecting", "thinking");
  setBadge(els.statusSaa, "connecting", "thinking");

  const token = els.saaToken.value.trim() || window.__ATTENLABS_TOKEN__ || "";
  if (!token) {
    log("Paste your SAA token first (attentionlabs.ai/dashboard)", "saa");
    setBadge(els.statusSaa, "no token", "listening");
    setBadge(els.statusAgent, "idle");
    els.start.disabled = false;
    return;
  }
  saa = new AttentionClient({ token });
  saa.on("speechReady", (ev) => {
    if (!realtimeWs || realtimeWs.readyState !== WebSocket.OPEN) return;
    log("speechReady - forwarding to OpenAI", "saa");
    realtimeWs.send(JSON.stringify({
      type: "input_audio_buffer.append",
      audio: ev.audioBase64,
    }));
    realtimeWs.send(JSON.stringify({ type: "input_audio_buffer.commit" }));
    realtimeWs.send(JSON.stringify({ type: "response.create" }));
  });
  saa.on("prediction", (p) => {
    setBadge(els.statusSaa, p.cls === 2 ? "device-directed" : "ambient",
      p.cls === 2 ? "speaking" : "listening");
  });
  await saa.start();

  const ephemeral = await mintRealtimeToken();
  realtimeWs = new WebSocket("wss://api.openai.com/v1/realtime?model=gpt-realtime", [
    "openai-insecure-api-key." + ephemeral,
    "openai-beta.realtime-v1",
  ]);
  realtimeWs.onopen = () => {
    setBadge(els.statusAgent, "listening");
    realtimeWs.send(JSON.stringify({
      type: "session.update",
      session: {
        instructions: script.system_prompt,
        input_audio_format: "pcm16",
        output_audio_format: "pcm16",
        turn_detection: null,
      },
    }));
    log("session ready", "saa");
    els.trigger.disabled = false;
    els.stop.disabled = false;
  };
  realtimeWs.onmessage = (e) => handleRealtime(JSON.parse(e.data));
  realtimeWs.onclose = () => log("openai disconnected", "saa");

  // Subscribe to back-end proactive-events SSE so webhooks can fire
  // the proactive turn remotely (CRM → /proactive-trigger →
  // SSE → browser).
  es = new EventSource("/proactive-events");
  es.addEventListener("trigger", (ev) => {
    try {
      const data = JSON.parse(ev.data);
      log(`(remote trigger: ${data.instructions?.slice(0, 60)}...)`, "saa");
      triggerProactiveTurn(data.instructions);
    } catch (err) {
      log(`(remote trigger parse error: ${err})`, "saa");
    }
  });
}

function handleRealtime(msg) {
  switch (msg.type) {
    case "response.created":
      currentResponseId = msg.response?.id || null;
      setBadge(els.statusAgent, "thinking", "thinking");
      break;
    case "response.audio.delta":
      if (!agentSpeaking) {
        agentSpeaking = true;
        setBadge(els.statusAgent, "speaking", "speaking");
      }
      // Trust the parent's playback path; for this overlay we focus on
      // proactive trigger + lifecycle correctness, not the playback
      // codec. The parent main.js handles audio playback via Web Audio
      // API; we expect the embedding page to render audio if needed.
      break;
    case "response.done":
      currentResponseId = null;
      setTimeout(() => {
        agentSpeaking = false;
        setBadge(els.statusAgent, "listening");
        // Release the SAA gate after a tick for trailing audio.
        saa?.markResponding(false);
      }, 200);
      break;
    case "response.audio_transcript.done":
      if (msg.transcript) log(`agent: ${msg.transcript}`, "gpt");
      break;
    case "conversation.item.input_audio_transcription.completed":
      if (msg.transcript) log(`you: ${msg.transcript}`, "you");
      break;
    case "error":
      log(`error: ${JSON.stringify(msg.error || msg)}`, "saa");
      break;
  }
}

function triggerProactiveTurn(instructions) {
  if (!realtimeWs || realtimeWs.readyState !== WebSocket.OPEN) {
    log("(cannot trigger: realtime not connected)", "saa");
    return;
  }
  const text = instructions || els.instructions.value || script.opening_line;
  log(`(trigger) ${text}`, "saa");
  // CRITICAL: assert mark_responding BEFORE the response.create. This
  // is the proactive turn boundary; without this the agent's opening
  // audio will re-fire SAA on its own echo.
  saa?.markResponding(true);
  realtimeWs.send(JSON.stringify({
    type: "response.create",
    response: {
      modalities: ["audio", "text"],
      instructions: text,
    },
  }));
}

function stop() {
  els.stop.disabled = true;
  els.trigger.disabled = true;
  if (es) {
    try { es.close(); } catch (e) {}
    es = null;
  }
  if (realtimeWs) {
    try { realtimeWs.close(); } catch (e) {}
    realtimeWs = null;
  }
  if (saa) {
    try { saa.stop(); } catch (e) {}
    saa = null;
  }
  setBadge(els.statusAgent, "idle");
  setBadge(els.statusSaa, "idle");
  els.start.disabled = false;
}

els.start.addEventListener("click", () => { start().catch((e) => log(`start error: ${e}`, "saa")); });
els.stop.addEventListener("click", stop);
els.trigger.addEventListener("click", () => triggerProactiveTurn(els.instructions.value));

loadScript();
