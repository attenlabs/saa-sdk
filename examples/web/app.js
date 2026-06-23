import { AttentionClient } from "saa-js";
import { RealtimeLLMBridge } from "./llm.js";

const CLASS_TO_STATE = { 0: "silent", 1: "human", 2: "device" };

const STATES = {
  silent:     { label: "SILENT",                short: true,  body: "state-silent" },
  human:      { label: "TALKING TO EACH OTHER", short: false, body: "state-human"  },
  device:     { label: "TALKING TO COMPUTER",   short: false, body: "state-device" },
  responding: { label: "AI IS RESPONDING",      short: false, body: "state-responding" },
};

const LLM_INSTRUCTIONS =
  "You are a helpful assistant. Respond concisely in 1 sentence. " +
  "If a device/TV command is spoken to you, respond as if you were controlling a TV.";

const GREETING_INSTRUCTIONS =
  "Greet the user warmly in one short sentence -> 'Hey there, what can I help you with today?'. English only";

// Hold mute + responding-state for a beat after playback ends so speakers /
// room reverb don't bleed into the mic and trigger a feedback loop.
const POST_PLAYBACK_MUTE_HOLD_MS = 400;

const SUGGESTIONS = [
  "Try talking to the computer",
  "Now try talking to each other",
  "Now test this however you want!",
];

const GUIDE_STEPS = {
  AWAITING_COMPUTER: 0,
  COMPUTER_DONE_WAITING_FOR_SILENCE: 1,
  AWAITING_HUMAN: 2,
  HUMAN_DONE_WAITING_FOR_SILENCE: 3,
  DONE: 4,
};

// URL params: ?server=… ?token=… ?openai_key=…
const params = new URLSearchParams(location.search);
const serverOverride = params.get("server") || undefined;
const urlToken = params.get("token");
const urlOpenai = params.get("openai_key");
const ENABLE_GREETING = !params.has("nogreet");

// ── DOM refs ────────────────────────────────────────────────────────────────
const authPanel    = document.getElementById("authPanel");
const inputToken   = document.getElementById("input-token");
const inputOpenai  = document.getElementById("input-openai");
const classNameEl  = document.getElementById("className");
const confPctEl    = document.getElementById("confPct");
const statFaces    = document.getElementById("statFaces");
const statVad      = document.getElementById("statVad");
const statConv     = document.getElementById("statConv");
const btn          = document.getElementById("btnConnect");
const warmupBarFill = document.getElementById("warmupBarFill");
const warmStepsEl  = document.getElementById("warmSteps");
const warmupPctEl  = document.getElementById("warmupPct");
const videoEl      = document.getElementById("videoEl");
const camPlaceholder = videoEl.previousElementSibling;
const threshSlider = document.getElementById("threshSlider");
const threshVal    = document.getElementById("threshVal");
const toastEl      = document.getElementById("toast");
const suggestionEl = document.getElementById("suggestion");
const suggestionTx = document.getElementById("suggestionText");
const tokensBlock  = document.getElementById("tokensBlock");
const tokensCount  = document.getElementById("tokensCount");

// ── Session state ──────────────────────────────────────────────────────────
let client = null;
let llm = null;
let running = false;
let warmedUp = false;
let llmActive = false;
let pred = { s: "silent", conf: 0, faces: 0 };
let vadStr = "--";
let convStr = "--";
let modelClass2Threshold = 0.85;

// Warmup progress: model needs ~50 ticks of audio/video history before it
// makes a confident prediction. Drive the staged warmup card off this count;
// the flip to warmedUp happens ONLY on the SDK's warmupComplete event.
const WARMUP_TICKS = 50;
let _predCount = 0;

// Rolling buffer of recent model predictions (newest first), shown as a small
// log under the stats. Compact labels mirror the livekit/pipecat web demos.
const PRED_BUFFER_MAX = 12;
const BUF_LABELS = { 0: "silent", 1: "human ↔ human", 2: "talking to me" };
const predBuffer = [];

let currentSuggestion = -1;
let guideStep = GUIDE_STEPS.AWAITING_COMPUTER;
// Set to true between warmupComplete and the first time the AI finishes
// speaking its greeting. Blocks the guided-suggestion flow during that
// window so the suggestion card doesn't pop up before the AI has said hello.
let greetingPending = false;
// Set to true between SAS turnReady (user finished an utterance — audio
// sent to OpenAI) and the matching LLM speakingEnd. Blocks the guided
// suggestion advance during the OpenAI processing+playback window so the
// next step doesn't reveal mid-response. Also keeps the orb reading
// "responding" through the silent-prediction gap before speakingStart.
let aiResponsePending = false;

// Pre-populate inputs from URL params (still editable).
if (urlToken)  inputToken.value  = urlToken;
if (urlOpenai) inputOpenai.value = urlOpenai;

// ── Toast ──────────────────────────────────────────────────────────────────
let toastTimer = null;
function toast(msg, ms = 5000) {
  toastEl.textContent = msg;
  toastEl.classList.add("visible");
  clearTimeout(toastTimer);
  if (ms > 0) toastTimer = setTimeout(() => toastEl.classList.remove("visible"), ms);
}
function clearToast() {
  toastEl.classList.remove("visible");
  clearTimeout(toastTimer);
}

// ── Tokens ticker ──────────────────────────────────────────────────────────
let tokensSaved = 0;
let tickerTimer = null;
let tickerRunning = false;

function formatTokens(n) {
  return String(Math.min(n, 9999)).padStart(4, "0");
}

function startTicker() {
  if (tickerRunning) return;
  tickerRunning = true;
  tokensBlock.classList.add("visible");
  const tick = () => {
    if (!tickerRunning) return;
    if (tokensSaved < 9999) {
      tokensSaved = Math.min(9999, tokensSaved + Math.floor(Math.random() * 15 + 8));
      tokensCount.textContent = formatTokens(tokensSaved);
      tokensCount.classList.remove("ticking");
      void tokensCount.offsetWidth;
      tokensCount.classList.add("ticking");
    }
    tickerTimer = setTimeout(tick, 180 + Math.random() * 120);
  };
  tick();
}

function pauseTicker() {
  tickerRunning = false;
  clearTimeout(tickerTimer);
}

function resetTicker() {
  pauseTicker();
  tokensSaved = 0;
  tokensCount.textContent = "0000";
  tokensBlock.classList.remove("visible");
}

// ── Suggestion / guide flow ────────────────────────────────────────────────
function setSuggestion(idx) {
  if (idx === currentSuggestion) return;
  currentSuggestion = idx;
  suggestionTx.classList.add("changing");
  setTimeout(() => {
    suggestionTx.textContent = SUGGESTIONS[idx];
    suggestionTx.classList.remove("changing");
  }, 300);
}

function showSuggestion(idx) {
  setSuggestion(idx);
  suggestionEl.classList.add("visible");
}

function hideSuggestion() {
  suggestionEl.classList.remove("visible");
}

function predictionPassesThreshold(p) {
  return p.conf >= modelClass2Threshold;
}

function updateGuidedPrompt(p) {
  // Don't advance the guide while:
  //   - the LLM is mid-playback (llmActive)
  //   - the initial greeting hasn't finished yet (greetingPending)
  //   - the AI hasn't yet answered the user's most recent utterance
  //     (aiResponsePending) — covers the OpenAI roundtrip window between
  //     turnReady and speakingStart, where the prediction is "silent"
  //     but the AI hasn't spoken yet, so naïvely advancing the guide
  //     would flash the next suggestion early.
  if (llmActive || greetingPending || aiResponsePending) return;

  const confident = predictionPassesThreshold(p);
  const isSilent = p.s === "silent";

  if (guideStep === GUIDE_STEPS.AWAITING_COMPUTER) {
    if (p.s === "device" && confident) {
      guideStep = GUIDE_STEPS.COMPUTER_DONE_WAITING_FOR_SILENCE;
      hideSuggestion();
      return;
    }
    showSuggestion(0);
    return;
  }

  if (guideStep === GUIDE_STEPS.COMPUTER_DONE_WAITING_FOR_SILENCE) {
    if (isSilent) {
      guideStep = GUIDE_STEPS.AWAITING_HUMAN;
      showSuggestion(1);
      return;
    }
    hideSuggestion();
    return;
  }

  if (guideStep === GUIDE_STEPS.AWAITING_HUMAN) {
    if (p.s === "human" && confident) {
      guideStep = GUIDE_STEPS.HUMAN_DONE_WAITING_FOR_SILENCE;
      hideSuggestion();
      return;
    }
    showSuggestion(1);
    return;
  }

  if (guideStep === GUIDE_STEPS.HUMAN_DONE_WAITING_FOR_SILENCE) {
    if (isSilent) {
      guideStep = GUIDE_STEPS.DONE;
      showSuggestion(2);
      return;
    }
    hideSuggestion();
    return;
  }

  showSuggestion(2);
}

// ── Warmup checklist + bar ─────────────────────────────────────────────────
function renderWarmup(count) {
  const pct = Math.min(1, count / WARMUP_TICKS);
  warmupBarFill.style.width = `${pct * 100}%`;
  warmupPctEl.textContent = `${Math.round(pct * 100)}%`;

  const lis = warmStepsEl.querySelectorAll("li");
  const stepSize = WARMUP_TICKS / lis.length;
  const activeIdx = Math.min(lis.length, Math.floor(count / stepSize));
  lis.forEach((li, i) => {
    li.classList.toggle("done",  i < activeIdx);
    li.classList.toggle("active", i === activeIdx && i < lis.length);
    const pctSpan = li.querySelector(".pct");
    if (pctSpan) pctSpan.textContent = i < activeIdx ? "100%" : i === activeIdx ? "…" : "";
  });
}

function finalizeWarmup() {
  warmupBarFill.style.width = "100%";
  warmupPctEl.textContent = "100%";
  warmStepsEl.querySelectorAll("li").forEach((li) => {
    li.classList.add("done");
    li.classList.remove("active");
    const pctSpan = li.querySelector(".pct");
    if (pctSpan) pctSpan.textContent = "100%";
  });
}

// ── Rolling prediction buffer ──────────────────────────────────────────────
function pushPredBuffer(e) {
  predBuffer.unshift({
    cls: e.cls,
    raw: e.rawCls,
    conf: e.confidence ?? 0,
    faces: e.numFaces ?? 0,
    responding: !!e.responding,
  });
  predBuffer.length = Math.min(predBuffer.length, PRED_BUFFER_MAX);
  renderPredBuffer();
}

function renderPredBuffer() {
  const ul = document.getElementById("predBuffer");
  if (!ul) return;
  ul.innerHTML = predBuffer
    .map((r) => {
      const label = r.responding ? "responding" : (BUF_LABELS[r.cls] ?? "?");
      const raw =
        !r.responding && r.raw != null && r.raw !== r.cls
          ? `<span class="buf-raw">(raw ${r.raw})</span>`
          : "";
      return (
        `<li data-cls="${r.cls}" data-responding="${r.responding}">` +
        `<span class="chip">${label}${raw}</span>` +
        `<span class="buf-conf">${Math.round(r.conf * 100)}%</span>` +
        `<span class="buf-faces">faces: ${r.faces}</span>` +
        `</li>`
      );
    })
    .join("");
}

// ── Render: rebuild visible UI from latest signals ─────────────────────────
function render() {
  // Treat the "speech sent → AI roundtrip" window as already-responding so
  // the orb doesn't visibly drop to SILENT for the 1-3 s between the user
  // finishing their turn and the LLM starting playback. aiResponsePending
  // is set the moment we ship audio to OpenAI and cleared on speakingEnd
  // (or on llm error). Same treatment for greetingPending so the orb stays
  // "responding" through the initial greeting.
  const displayS = (llmActive || aiResponsePending || greetingPending)
    ? "responding"
    : (warmedUp ? pred.s : "silent");
  const st = STATES[displayS];

  // warm up state while connected + but not warmed up
  document.body.className = st.body + (running && !warmedUp ? " warming-up" : "");
  classNameEl.classList.toggle("short", st.short);

  if (!running) {
    classNameEl.textContent = "NOT CONNECTED";
    confPctEl.textContent = "--";
    statFaces.textContent = "--";
    statVad.textContent = "--";
    statConv.textContent = "--";
    hideSuggestion();
    return;
  }

  if (!warmedUp) {
    classNameEl.textContent = "WARMING UP";
    classNameEl.classList.remove("short");
    confPctEl.textContent = "--";
    statFaces.textContent = pred.faces || "--";
    statVad.textContent = vadStr;
    statConv.textContent = convStr;
    hideSuggestion();
    return;
  }

  classNameEl.textContent = st.label;
  confPctEl.textContent = pred.conf > 0 ? Math.round(pred.conf * 100) + "%" : "--";
  statFaces.textContent = pred.faces ?? "--";
  statVad.textContent = vadStr;
  statConv.textContent = convStr;

  // Tokens ticker runs only when speech is human-directed (not at the device).
  if (!llmActive && pred.s === "human" && pred.conf > 0.3) {
    startTicker();
  } else {
    pauseTicker();
  }

  updateGuidedPrompt(pred);
}

// ── Threshold ──────────────────────────────────────────────────────────────
function setThresholdFromSlider() {
  modelClass2Threshold = Number(threshSlider.value) / 100;
  threshVal.textContent = modelClass2Threshold.toFixed(2);
  if (client) client.setThreshold(modelClass2Threshold);
}
threshSlider.addEventListener("input", setThresholdFromSlider);

// ── Connect button gating ──────────────────────────────────────────────────
function refreshConnectButton() {
  if (running) return;
  btn.disabled = !inputToken.value.trim();
}
inputToken.addEventListener("input", refreshConnectButton);
refreshConnectButton();

btn.addEventListener("click", () => running ? stop() : start());

// ── Lifecycle ──────────────────────────────────────────────────────────────
async function start() {
  const token = inputToken.value.trim();
  const openaiKey = inputOpenai.value.trim() || null;
  if (!token) { toast("Enter a SAA token to connect."); return; }

  btn.disabled = true;
  btn.textContent = "Connecting…";
  clearToast();
  authPanel.classList.add("hidden");

  // Reset session state.
  warmedUp = false;
  llmActive = false;
  greetingPending = false;
  aiResponsePending = false;
  _predCount = 0;
  renderWarmup(0);
  predBuffer.length = 0;
  renderPredBuffer();
  pred = { s: "silent", conf: 0, faces: 0 };
  vadStr = "--";
  convStr = "--";
  currentSuggestion = -1;
  guideStep = GUIDE_STEPS.AWAITING_COMPUTER;
  resetTicker();

  client = new AttentionClient({
    url: serverOverride,
    token,
    initialThreshold: modelClass2Threshold,
  });

  client.on("connected", () => {
    running = true;
    btn.disabled = false;
    btn.textContent = "Disconnect";
    btn.classList.add("stop");
    videoEl.style.display = "block";
    if (camPlaceholder) camPlaceholder.style.display = "none";
    render();
  });

  client.on("warmupComplete", () => {
    warmedUp = true;
    finalizeWarmup();
    // Disable with ?nogreet for diagnostics (e.g. when chasing feedback loops).
    // When the greeting fires we block the suggestion card with greetingPending
    // until the AI has finished its hello — otherwise the first post-warmup
    // prediction shows suggestion 0 ahead of the AI. When there's no greeting
    // path (no key / ?nogreet), reveal immediately.
    if (llm && ENABLE_GREETING) {
      greetingPending = true;
      llm.greet(GREETING_INSTRUCTIONS);
    } else {
      showSuggestion(0);
    }
    render();
  });

  client.on("prediction", (e) => {
    if (!warmedUp) {
      _predCount++;
      renderWarmup(_predCount);
    } else {
      pushPredBuffer(e); // log real post-warmup predictions
    }
    if (llmActive) return;
    const s = CLASS_TO_STATE[e.cls] ?? "silent";
    const newPred = { s, conf: e.confidence ?? 0, faces: e.numFaces ?? 0 };
    // Hold the last non-silent snapshot while the server is mid-utterance
    // (Listening/Sending). Otherwise the orb flickers SILENT in the gap
    // between the user finishing their turn and turnReady arriving.
    const inFlight = convStr === "Listening" || convStr === "Sending";
    if (inFlight && newPred.s === "silent" && pred.s !== "silent") {
      pred = { ...pred, faces: newPred.faces };
    } else {
      pred = newPred;
    }
    render();
  });

  client.on("vad", (e) => {
    vadStr = e.probability != null ? `${Math.round(e.probability * 100)}%` : "--";
    render();
  });

  client.on("state", (e) => {
    const map = { listening: "Listening", sending: "Sending", cancelled: "Idle", idle: "Idle" };
    convStr = map[e.state] ?? e.state ?? "--";
    render();
  });

  client.on("turnReady", (e) => {
    convStr = "Idle";
    if (llm) {
      // Mark the guide as awaiting an AI response so the silent-prediction
      // gap between now and speakingStart can't trigger a suggestion advance
      // (or drop the orb to SILENT). Cleared in the LLM speakingEnd handler.
      aiResponsePending = true;
      llm.sendAudioB64(e.audioBase64, e.frames ?? []);
    }
    render();
  });

  client.on("config", (e) => {
    if (typeof e.modelClass2Threshold === "number") {
      modelClass2Threshold = e.modelClass2Threshold;
      threshSlider.value = String(Math.round(modelClass2Threshold * 100));
      threshVal.textContent = modelClass2Threshold.toFixed(2);
    }
  });

  client.on("interrupt", (e) => {
    // SAS server's InterruptDetector caught the user barging in during AI
    // playback. The server has already (a) flipped is_responding=False,
    // (b) moved its state machine into LISTENING, (c) pre-rolled the
    // barge-in audio into the chunk accumulator so the next turn carries
    // the user's actual question, and (d) sent state:listening. Mirror
    // that on the client immediately — if we wait for llm.interrupt()'s
    // fade + the speakingEnd setTimeout, the mic stays muted for ~900ms
    // and the orb keeps reading "responding" through what the user
    // experiences as a successful barge-in.
    if (llm) llm.interrupt(e.fadeMs);
    llmActive = false;
    aiResponsePending = false;
    greetingPending = false;
    // pred mirrors the firing tick's class-2 prediction so the orb flips
    // to "device" immediately (predictions are dropped client-side while
    // llmActive=true, so without this it'd fall back to a stale value).
    pred = {
      s: "device",
      conf: typeof e.confidence === "number" ? e.confidence : 0.85,
      faces: pred.faces,
    };
    if (client) {
      client.unmute();
      client.markResponding(false);
    }
    render();
  });

  client.on("error", (e) => {
    toast(`${e.title || "Error"}: ${e.message}`, 0);
  });

  client.on("disconnected", (e) => {
    if (running && e.code !== 1000) {
      const reason = e.code === 1008 ? "auth rejected"
                   : e.code === 1013 ? "rate limited"
                   : e.code === 1006 ? "connection failed"
                   : e.reason || `closed (code ${e.code})`;
      toast(`Disconnected — ${reason}`, 0);
    }
    stop();
  });

  if (openaiKey) {
    llm = new RealtimeLLMBridge({ apiKey: openaiKey, instructions: LLM_INSTRUCTIONS });
    // Open the OpenAI Realtime WS now so the handshake + session.update finish
    // during the ~12.5s SAS warmup window. greet() at warmupComplete then only
    // pays model-generation latency, not connect latency.
    llm.prewarm();
    llm.on("speakingStart", () => {
      llmActive = true;
      if (client) { client.mute(); client.markResponding(true); }
      render();
    });
    llm.on("speakingEnd", () => {
      // Release the suggestion-card gates as soon as audio finishes — the
      // mute-hold below is purely for echo suppression, not UI state.
      if (greetingPending) {
        greetingPending = false;
        // First reveal of suggestion 0 happens after the greeting wraps,
        // not at warmupComplete — see warmupComplete handler.
        showSuggestion(0);
      }
      if (aiResponsePending) aiResponsePending = false;
      // Hold mute briefly after playback so the speaker tail / room reverb
      // doesn't get re-detected as device speech and loop us back into the LLM.
      setTimeout(() => {
        llmActive = false;
        if (client) { client.unmute(); client.markResponding(false); }
        render();
      }, POST_PLAYBACK_MUTE_HOLD_MS);
    });
    llm.on("error", (e) => {
      toast(`LLM ${e.title || "error"}: ${e.message}`);
      llmActive = false;
      greetingPending = false;
      aiResponsePending = false;
      if (client) { client.unmute(); client.markResponding(false); }
      render();
    });
  }

  try {
    await client.start({ videoElement: videoEl });
  } catch (err) {
    toast(`Start failed: ${err?.message || err}`, 0);
    stop();
  }
}

async function stop() {
  running = false;
  if (client) { try { await client.stop(); } catch {} client = null; }
  if (llm)    { llm.close(); llm = null; }

  warmedUp = false;
  llmActive = false;
  greetingPending = false;
  aiResponsePending = false;
  pred = { s: "silent", conf: 0, faces: 0 };
  vadStr = "--";
  convStr = "--";
  currentSuggestion = -1;
  guideStep = GUIDE_STEPS.AWAITING_COMPUTER;
  resetTicker();
  _predCount = 0;
  renderWarmup(0);
  predBuffer.length = 0;
  renderPredBuffer();

  videoEl.style.display = "none";
  if (camPlaceholder) camPlaceholder.style.display = "flex";
  videoEl.srcObject = null;

  authPanel.classList.remove("hidden");
  btn.disabled = !inputToken.value.trim();
  btn.textContent = "Connect";
  btn.classList.remove("stop");
  render();
}

// Initial paint.
render();
