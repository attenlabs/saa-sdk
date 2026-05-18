// Proactive variant of examples/elevenlabs-cai/main.js.
//
// The reactive base wires SAA's speechReady to ElevenLabs CAI's
// setMicMuted / sendContextualUpdate flow. This proactive variant adds:
//
//   1. A "Trigger proactive turn" button (and an EventSource listening
//      on /proactive-events for back-end-triggered turns) that asserts
//      markResponding(true) and uses sendUserMessage to synthesise a
//      user turn that prompts the agent to speak.
//   2. markResponding(false) is asserted when the agent's onMessage
//      reports the agent has finished its turn.
//
// Why sendUserMessage instead of sendContextualUpdate: contextualUpdate
// is annotation only (agent doesn't necessarily respond). sendUserMessage
// makes the agent treat it as if the user said something, so the agent
// is forced to reply - which is the proactive turn from the user's POV.
// The "user" is the back-end / scheduler / CRM, not the human.

// CDN-imported to mirror examples/elevenlabs-cai/main.js:41-42 exactly.
import { AttentionClient } from "https://esm.sh/@attenlabs/saa-js@0.3.0";
import { Conversation } from "https://esm.sh/@elevenlabs/client@^1.7.0";

let saa = null;
let convo = null;
let es = null;
let script = {
  opening_line: "Hi - want me to walk you through what's new?",
  system_prompt: "You are a helpful proactive assistant.",
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
    const res = await fetch("./demo_script.json");
    if (res.ok) {
      script = await res.json();
      els.instructions.value = script.opening_line;
    }
  } catch (e) {
    log("(could not load demo_script.json)", "saa");
  }
}

async function start() {
  els.start.disabled = true;
  setBadge(els.statusAgent, "connecting", "thinking");
  setBadge(els.statusSaa, "connecting", "thinking");

  // SAA client. The user pastes their token into the saaToken input;
  // window.__ATTENLABS_TOKEN__ is kept as a fallback for server-injected
  // tokens (e.g. via meta-tag or server-side template).
  const token = els.saaToken.value.trim() || window.__ATTENLABS_TOKEN__ || "";
  if (!token) {
    log("Paste your SAA token first (attentionlabs.ai/dashboard)", "saa");
    setBadge(els.statusSaa, "no token", "listening");
    setBadge(els.statusAgent, "idle");
    els.start.disabled = false;
    return;
  }
  saa = new AttentionClient({ token });
  saa.on("prediction", (p) => {
    setBadge(els.statusSaa, p.cls === 2 ? "device-directed" : "ambient",
      p.cls === 2 ? "speaking" : "listening");
  });
  await saa.start();

  // ElevenLabs CAI session. The relay mints a signed URL or
  // conversation-token; mirror examples/elevenlabs-cai/server.py.
  const tokenRes = await fetch("/api/conversation-token");
  const tokenData = await tokenRes.json();
  convo = await Conversation.startSession({
    agentId: tokenData.agent_id,
    signedUrl: tokenData.signed_url,
    onMessage: handleAgentMessage,
    onStatusChange: ({ status }) => log(`(cai status: ${status})`, "saa"),
    onModeChange: handleModeChange,
  });

  setBadge(els.statusAgent, "listening");
  els.trigger.disabled = false;
  els.stop.disabled = false;

  // Subscribe to remote triggers via SSE.
  es = new EventSource("/proactive-events");
  es.addEventListener("trigger", (ev) => {
    try {
      const data = JSON.parse(ev.data);
      log(`(remote trigger: ${data.instructions?.slice(0, 60)}...)`, "saa");
      triggerProactiveTurn(data.instructions);
    } catch (err) {
      log(`(trigger parse error: ${err})`, "saa");
    }
  });
}

function handleAgentMessage(msg) {
  if (msg.source === "user" && msg.message) {
    log(`you: ${msg.message}`, "you");
  } else if (msg.source === "ai" && msg.message) {
    log(`agent: ${msg.message}`, "agent");
  }
}

function handleModeChange({ mode }) {
  // ElevenLabs CAI reports mode = "speaking" while the agent is
  // talking, "listening" otherwise. Mirror it onto SAA so the cloud
  // classifier suppresses predictions during the agent's TTS turn.
  if (mode === "speaking") {
    setBadge(els.statusAgent, "speaking", "speaking");
    saa?.markResponding(true);
  } else {
    setBadge(els.statusAgent, "listening");
    saa?.markResponding(false);
  }
}

function triggerProactiveTurn(instructions) {
  if (!convo) {
    log("(cannot trigger: not connected)", "saa");
    return;
  }
  const text = instructions || els.instructions.value || script.opening_line;
  log(`(trigger) ${text}`, "saa");
  // Assert mark_responding BEFORE we tell the agent to speak. The
  // handleModeChange callback will re-assert it on the mode transition,
  // but this catches the gap before the WebSocket round-trip.
  saa?.markResponding(true);
  // sendContextualUpdate seeds context for the next agent turn; the
  // ElevenLabs CAI client treats the prompt below as a synthetic user
  // message so the agent is forced to reply.
  try {
    if (typeof convo.sendUserMessage === "function") {
      convo.sendUserMessage(text);
    } else {
      convo.sendContextualUpdate(`[proactive trigger] ${text}`);
    }
  } catch (err) {
    log(`(trigger failed: ${err?.message ?? err})`, "saa");
  }
}

function stop() {
  els.stop.disabled = true;
  els.trigger.disabled = true;
  if (es) {
    try { es.close(); } catch (e) {}
    es = null;
  }
  if (convo) {
    try { convo.endSession(); } catch (e) {}
    convo = null;
  }
  if (saa) {
    try { saa.stop(); } catch (e) {}
    saa = null;
  }
  setBadge(els.statusAgent, "idle");
  setBadge(els.statusSaa, "idle");
  els.start.disabled = false;
}

els.start.addEventListener("click", () => start().catch((e) => log(`start error: ${e}`, "saa")));
els.stop.addEventListener("click", stop);
els.trigger.addEventListener("click", () => triggerProactiveTurn(els.instructions.value));

loadScript();
