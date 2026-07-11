// SAA + Stream Video browser client — no build step
// SAA runs browser-side (no Python bot participant in Stream Video);
// mirrors the pipecat example structure but uses AttentionClient events
// instead of Daily app-messages.
import { AttentionClient } from "saa-js";
import { StreamVideoClient } from "@stream-io/video-client";
import { RealtimeLLMBridge } from "./llm.js";

const LABELS = { 0: "silent", 1: "human ↔ human", 2: "talking to me" };
const LLM_INSTRUCTIONS =
  "You are a helpful assistant. Respond concisely in 1 sentence. " +
  "If a device/TV command is spoken, respond as if you were controlling a TV.";
const PRED_BUFFER_MAX = 12;

let saa = null;
let streamClient = null;
let call = null;
let llm = null;
let llmSpeaking = false;
const callUnsubs = [];

const predBuffer = [];
let _lastClass = null;
let _lastVad = null;

// ── DOM refs ────────────────────────────────────────────────────────────────
const inputToken   = document.getElementById("input-token");
const inputOpenai  = document.getElementById("input-openai");
const threshSlider = document.getElementById("thresh-slider");
const threshValEl  = document.getElementById("thresh-val");

// URL param pre-fill (useful for dev links)
const params = new URLSearchParams(location.search);
if (params.get("token"))      inputToken.value  = params.get("token");
if (params.get("openai_key")) inputOpenai.value = params.get("openai_key");

// Pre-fetch /config — no side effects, marks server-configured fields green
(async () => {
  try {
    const cfg = await fetch("/config").then(r => r.json());
    if (cfg.saaConfigured && !inputToken.value) {
      inputToken.placeholder = "✓ configured via server .env";
      inputToken.closest(".config-field")?.classList.add("server-configured");
    }
    if (cfg.openaiConfigured && !inputOpenai.value) {
      inputOpenai.placeholder = "✓ configured via server .env";
      inputOpenai.closest(".config-field")?.classList.add("server-configured");
    }
  } catch {}
})();

document.getElementById("btn-start").onclick = start;
document.getElementById("btn-stop").onclick  = stop;

threshSlider.addEventListener("input", () => {
  const t = Number(threshSlider.value) / 100;
  threshValEl.textContent = t.toFixed(2);
  if (saa) saa.setThreshold(t);
});

// ── Start ────────────────────────────────────────────────────────────────────
async function start() {
  document.getElementById("btn-start").disabled = true;
  setStatus("requesting session…");

  // fetch Stream credentials + server-side call creation
  let session;
  try {
    const r = await fetch("/session");
    if (!r.ok) throw new Error(`/session ${r.status}: ${await r.text()}`);
    session = await r.json();
  } catch (err) {
    setStatus(`error: ${err.message}`);
    showError(
      `Could not start a session: ${err.message}\n\n` +
      `Check that token_server.py is running and STREAM_API_KEY + SAA_API_KEY are set in .env.`,
    );
    document.getElementById("btn-start").disabled = false;
    return;
  }

  const saaToken  = session.saaToken  || inputToken.value.trim();
  const openaiKey = inputOpenai.value.trim() || session.openaiApiKey || null;

  if (!saaToken) {
    setStatus("error: no SAA token");
    showError("No SAA token found. Set SAA_API_KEY in .env or enter it above.");
    document.getElementById("btn-start").disabled = false;
    return;
  }

  clearError();

  // Wire up LLM bridge if an OpenAI key is available
  if (openaiKey) {
    llm = new RealtimeLLMBridge({ apiKey: openaiKey, instructions: LLM_INSTRUCTIONS });
    llm.prewarm();
    llm.on("speakingStart", () => {
      llmSpeaking = true;
      if (saa) { saa.mute(); saa.markResponding(true); }
      setResponding(true);
    });
    llm.on("speakingEnd", () => {
      setTimeout(() => {
        llmSpeaking = false;
        if (saa) { saa.unmute(); saa.markResponding(false); }
        setResponding(false);
      }, 400);
    });
    llm.on("error", e => console.warn("[llm]", e.message));
    setMode("with voice agent");
  } else {
    setMode("overlay only");
  }

  // Start SAA (browser-side attention model)
  const threshold = Number(threshSlider.value) / 100;
  saa = new AttentionClient({ token: saaToken, initialThreshold: threshold });

  saa.on("connected", () => {
    setWarming(true);
    setStatus("warming up…");
    document.getElementById("btn-stop").disabled = false;
    // Join the Stream Video call as soon as SAA is connected
    joinCall(session);
  });

  saa.on("warmupComplete", () => {
    setWarming(false);
    setStatus("live");
    if (llm) llm.greet("Greet the user warmly in one short sentence. English only.");
  });

  saa.on("prediction", e => renderPrediction(e));
  saa.on("vad",        e => renderVAD(e));

  saa.on("turnReady", e => {
    if (llm) llm.sendAudioB64(e.audioBase64, e.frames ?? []);
  });

  saa.on("interrupt", e => {
    if (llm) llm.interrupt(e.fadeMs);
    llmSpeaking = false;
    setResponding(false);
    if (saa) { saa.unmute(); saa.markResponding(false); }
  });

  saa.on("config", e => {
    if (typeof e.modelClass2Threshold === "number") {
      threshSlider.value = String(Math.round(e.modelClass2Threshold * 100));
      threshValEl.textContent = e.modelClass2Threshold.toFixed(2);
    }
  });

  saa.on("error",        e => setStatus(`error: ${e.title || e.message}`));
  saa.on("disconnected", e => { if (e.code !== 1000) stop(); });

  try {
    await saa.start({ videoElement: document.getElementById("local-video") });
  } catch (err) {
    setStatus(`error: ${err.message}`);
    showError(`SAA failed to start: ${err.message}`);
    document.getElementById("btn-start").disabled = false;
    if (llm) { llm.close(); llm = null; }
  }
}

// ── Join Stream Video call ──────────────────────────────────────────────────
async function joinCall(session) {
  const { callId, callType, userId, userToken, streamApiKey } = session;
  try {
    streamClient = new StreamVideoClient({
      apiKey: streamApiKey,
      user: { id: userId, name: userId },
      token: userToken,
    });
    call = streamClient.call(callType ?? "default", callId);
    await call.join({ create: true });
    await call.camera.enable();
    await call.microphone.enable();

    const unsub = call.state.participants$.subscribe(pts => {
      document.getElementById("participants").textContent =
        `${pts.length} participant${pts.length !== 1 ? "s" : ""}`;
    });
    callUnsubs.push(unsub);
  } catch (err) {
    console.warn("[stream] join failed:", err.message);
  }
}

// ── Stop ────────────────────────────────────────────────────────────────────
async function stop() {
  for (const u of callUnsubs.splice(0)) {
    try { typeof u === "function" ? u() : u?.unsubscribe?.(); } catch {}
  }
  if (call)         { try { await call.leave();             } catch {} call = null; }
  if (streamClient) { try { await streamClient.disconnectUser(); } catch {} streamClient = null; }
  if (saa)          { try { await saa.stop();               } catch {} saa = null; }
  if (llm)          { llm.close(); llm = null; }
  llmSpeaking = false;

  // reset prediction card
  const pred = document.getElementById("prediction");
  pred.dataset.warming   = "false";
  pred.dataset.responding = "false";
  pred.dataset.class     = "0";
  document.getElementById("class-label").textContent = "--";
  document.getElementById("conf-fill").style.width   = "0%";
  document.getElementById("conf-num").textContent    = "0%";
  document.getElementById("vad").textContent         = "VAD: off";
  document.getElementById("faces").textContent       = "faces: 0";
  document.getElementById("participants").textContent = "";
  document.getElementById("local-video").srcObject   = null;
  predBuffer.length = 0;
  renderPredBuffer();
  _lastClass = null;
  _lastVad   = null;

  document.getElementById("btn-start").disabled = false;
  document.getElementById("btn-stop").disabled  = true;
  setStatus("disconnected");
}

// ── Render helpers ───────────────────────────────────────────────────────────
function setWarming(on) {
  const el = document.getElementById("prediction");
  el.dataset.warming = String(on);
  if (on) {
    el.dataset.class      = "0";
    el.dataset.responding = "false";
    document.getElementById("class-label").textContent = "warming up";
    document.getElementById("conf-fill").style.width   = "";
    document.getElementById("conf-num").textContent    = "—";
  } else {
    document.getElementById("class-label").textContent = "—";
    document.getElementById("conf-fill").style.width   = "0%";
    document.getElementById("conf-num").textContent    = "0%";
  }
}

function setResponding(on) {
  document.getElementById("prediction").dataset.responding = String(on);
  if (on) document.getElementById("class-label").textContent = "responding";
}

function renderPrediction(e) {
  const el = document.getElementById("prediction");
  if (el.dataset.warming === "true") return;
  const cls        = e.cls ?? 0;
  const responding = llmSpeaking || !!e.responding;
  const label      = responding ? "responding" : (LABELS[cls] ?? "?");
  document.getElementById("class-label").textContent     = label;
  const confPct = Math.round((e.confidence ?? 0) * 100);
  document.getElementById("conf-fill").style.width       = `${confPct}%`;
  document.getElementById("conf-num").textContent        = `${confPct}%`;
  document.getElementById("faces").textContent           = `faces: ${e.numFaces ?? 0}`;
  el.dataset.class      = String(cls);
  el.dataset.responding = String(responding);
  if (cls !== _lastClass) { _lastClass = cls; }
  pushPredBuffer(e, responding);
}

function pushPredBuffer(e, responding) {
  predBuffer.unshift({
    cls: e.cls ?? 0, raw: e.rawCls,
    conf: e.confidence ?? 0, faces: e.numFaces ?? 0, responding,
  });
  predBuffer.length = Math.min(predBuffer.length, PRED_BUFFER_MAX);
  renderPredBuffer();
}

function renderPredBuffer() {
  const ul = document.getElementById("pred-buffer");
  if (!ul) return;
  ul.innerHTML = predBuffer.map(r => {
    const label = r.responding ? "responding" : (LABELS[r.cls] ?? "?");
    const raw   = (!r.responding && r.raw != null && r.raw !== r.cls)
      ? `<span class="buf-raw">(raw ${r.raw})</span>` : "";
    return (
      `<li data-cls="${r.cls}" data-responding="${r.responding}">` +
      `<span class="chip">${label}${raw}</span>` +
      `<span class="buf-conf">${Math.round(r.conf * 100)}%</span>` +
      `<span class="buf-faces">faces: ${r.faces}</span></li>`
    );
  }).join("");
}

function renderVAD(v) {
  const on = (v.probability ?? 0) > 0.5;
  document.getElementById("vad").textContent = `VAD: ${on ? "on" : "off"}`;
  if (on !== _lastVad) { _lastVad = on; }
}

function setStatus(s) {
  document.getElementById("status").textContent = s;
}

function setMode(s) {
  let el = document.getElementById("mode");
  if (!el) {
    el = document.createElement("span");
    el.id        = "mode";
    el.className = "status";
    el.style.marginLeft = "8px";
    document.getElementById("status").after(el);
  }
  el.textContent = s;
}

function showError(msg) {
  let el = document.getElementById("error-banner");
  if (!el) {
    el = document.createElement("pre");
    el.id = "error-banner";
    el.style.cssText =
      "white-space:pre-wrap;background:#2a0e0e;color:#ffb4b4;" +
      "padding:12px;border-radius:8px;margin:12px 0;font-size:12px;";
    document.getElementById("root").insertBefore(
      el, document.querySelector(".controls"),
    );
  }
  el.textContent = msg;
}

function clearError() {
  document.getElementById("error-banner")?.remove();
}
