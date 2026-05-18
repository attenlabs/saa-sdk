// cloud-live-demo/main.js
//
// Public 60-second SAA Cloud demo. No dashboard account, no token paste.
//
// Flow:
//   1. POST to the demo-token endpoint (configurable; default same-origin /api/demo-token).
//   2. If it returns { token, expires_in_sec }, create an AttentionClient and stream
//      mic + camera to wss://server.attentionlabs.ai/ws for up to 60 s.
//   3. If it returns { ready: false } or any non-2xx, surface the dashboard-token
//      fallback banner and link out to the dashboard. No silent degradation.
//   4. Hard timeout at expires_in_sec (default 60 s) regardless of server cap.
//
// The SDK is loaded from esm.sh so this page is buildless and host-anywhere.

const CONFIG = (() => {
  // Allow overriding the API base from the URL fragment (#api=https://example.com)
  // or from a meta tag <meta name="saa-api-base" content="...">.
  let apiBase = "";
  const meta = document.querySelector('meta[name="saa-api-base"]');
  if (meta && meta.content) apiBase = meta.content.replace(/\/+$/, "");
  const frag = new URLSearchParams(location.hash.slice(1));
  if (frag.get("api")) apiBase = frag.get("api").replace(/\/+$/, "");
  return {
    demoTokenUrl: (apiBase || "") + "/api/demo-token",
    // The wire URL the SDK will dial; demo-token response can override.
    wsUrl: "wss://server.attentionlabs.ai/ws",
    sessionMaxSec: 60,
    threshold: 0.7,
    // Pinned to the currently-published npm version. Bump to @1.0.0 at launch.
    sdkUrl: "https://esm.sh/@attenlabs/saa-js@0.3.0",
  };
})();

// --- DOM ---------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const els = {
  comingSoon: $("comingsoon"),
  comingSoonReason: $("comingsoonReason"),
  video: $("video"),
  waveform: $("waveform"),
  gate: $("gate"),
  startBtn: $("startBtn"),
  stopBtn: $("stopBtn"),
  status: $("status"),
  countdown: $("countdown"),
  latencyBadge: $("latencyBadge"),
  faceBadge: $("faceBadge"),
  sourceBadge: $("sourceBadge"),
  predPill: $("predPill"),
  predConf: $("predConf"),
  predBarFill: $("predBarFill"),
  predBarThreshold: $("predBarThreshold"),
  thresholdLabel: $("thresholdLabel"),
  vadPill: $("vadPill"),
  statePill: $("statePill"),
  statRtt: $("statRtt"),
  statPreds: $("statPreds"),
  statAudio: $("statAudio"),
  statVideo: $("statVideo"),
  a11yPred: $("a11yPred"),
};

const LABELS = { 0: "silent", 1: "human-directed", 2: "device-directed" };

let client = null;
let sessionTimer = null;
let countdownTimer = null;
let sessionEndsAt = 0;
let predCount = 0;
let meterCtx = null;
let meterAnalyser = null;
let meterRaf = 0;
const meterBuf = new Float32Array(2048);

function setStatus(text, kind = "") {
  els.status.textContent = text;
  els.status.classList.remove("error", "ok");
  if (kind === "error") els.status.classList.add("error");
  if (kind === "ok") els.status.classList.add("ok");
}

function showComingSoon(reason) {
  if (reason) els.comingSoonReason.textContent = reason;
  els.comingSoon.dataset.visible = "true";
  els.gate.hidden = true;
  setStatus("Token mint gated on this host. Use a dashboard token to run the same demo end-to-end.");
}

function showStartGate() {
  els.gate.hidden = false;
  els.startBtn.disabled = false;
}

// --- demo-token preflight ----------------------------------------------------
//
// Server contract (see README § Server contract):
//   POST /api/demo-token
//   200 { "token": "<jwt>", "expires_in_sec": 60, "ws_url"?: "wss://..." }
//   429 { "retry_after_sec": N, "reason": "rate-limited" }
//   503 { "ready": false, "reason": "capacity" }
//   404 — endpoint not deployed on this host (dashboard-token fallback shown)
//
async function fetchDemoToken() {
  let res;
  try {
    res = await fetch(CONFIG.demoTokenUrl, {
      method: "POST",
      headers: { "Accept": "application/json" },
      cache: "no-store",
    });
  } catch (err) {
    return { ok: false, reason: `Couldn't reach ${CONFIG.demoTokenUrl}: ${err.message}` };
  }
  let body = null;
  try { body = await res.json(); } catch (_) { /* may be empty */ }

  if (res.status === 200 && body && body.token) {
    return {
      ok: true,
      token: body.token,
      expiresInSec: clamp(body.expires_in_sec ?? CONFIG.sessionMaxSec, 5, 120),
      wsUrl: body.ws_url || CONFIG.wsUrl,
    };
  }
  if (res.status === 429) {
    const wait = body?.retry_after_sec ?? 300;
    const mins = Math.ceil(wait / 60);
    return { ok: false, reason: `Rate-limited — try again in ${mins} minute${mins === 1 ? "" : "s"}. The demo allows one session per IP every 5 minutes.` };
  }
  if (res.status === 503 || body?.ready === false) {
    return { ok: false, reason: body?.reason || "Demo capacity is full right now. Try again in a minute, or grab a token at /dashboard for unlimited use." };
  }
  if (res.status === 404) {
    return { ok: false, reason: "The public demo-token service isn't deployed at this URL yet. Run the page against a server that implements /api/demo-token, or get a dashboard token at https://attentionlabs.ai/dashboard." };
  }
  return { ok: false, reason: `Demo-token service returned ${res.status}. Try the dashboard token path instead.` };
}

function clamp(n, lo, hi) { return Math.min(hi, Math.max(lo, Number(n) || lo)); }

// --- AttentionClient wiring --------------------------------------------------
let AttentionClient = null;
async function loadSDK() {
  try {
    const mod = await import(CONFIG.sdkUrl);
    AttentionClient = mod.AttentionClient;
  } catch (err) {
    showComingSoon(`Couldn't load the SDK from ${CONFIG.sdkUrl}. You're either offline or the CDN is rate-limiting.`);
    throw err;
  }
}

function setRunning(running) {
  els.startBtn.disabled = running;
  els.stopBtn.disabled = !running;
  els.gate.hidden = running;
}

function wireClient(c) {
  c.on("connected", () => setStatus("Connected to the inference server. Warming up…"));
  c.on("started", () => setStatus("Live. Speak naturally; look away to flip class to human-directed.", "ok"));
  c.on("warmupComplete", () => setStatus("Warm. The model is now active.", "ok"));

  c.on("prediction", (e) => {
    predCount++;
    const cls = e.cls ?? 0;
    const conf = Number.isFinite(e.confidence) ? e.confidence : 0;
    const label = LABELS[cls] ?? `cls-${cls}`;
    els.predPill.textContent = label;
    els.predPill.className = `pred-pill cls-${cls}`;
    els.predConf.innerHTML = `confidence <strong>${conf.toFixed(2)}</strong>`;
    els.predBarFill.style.width = `${Math.round(conf * 100)}%`;
    els.predBarFill.style.background =
      cls === 2 ? "var(--green)" : cls === 1 ? "var(--yellow)" : "var(--accent)";
    els.statPreds.textContent = String(predCount);
    if (e.source) els.sourceBadge.textContent = `source ${e.source}`;
    if (Number.isFinite(e.numFaces)) {
      const n = e.numFaces;
      els.faceBadge.textContent = `faces ${n}`;
      els.faceBadge.classList.toggle("ok", n > 0);
      els.faceBadge.classList.toggle("warn", n === 0);
    }
    els.a11yPred.textContent = `${label} at confidence ${conf.toFixed(2)}.`;
  });

  c.on("vad", (e) => {
    if (e.isSpeech) {
      els.vadPill.textContent = `speech ${e.probability.toFixed(2)}`;
      els.vadPill.className = "vad-pill speech";
    } else {
      els.vadPill.textContent = `silent ${e.probability.toFixed(2)}`;
      els.vadPill.className = "vad-pill";
    }
  });

  c.on("state", (e) => {
    els.statePill.textContent = e.state;
    els.statePill.className = `state-pill ${e.state}`;
  });

  c.on("config", (e) => {
    const t = e.modelClass2Threshold;
    if (Number.isFinite(t)) {
      els.predBarThreshold.style.left = `${Math.round(t * 100)}%`;
      els.thresholdLabel.textContent = `threshold τ = ${t.toFixed(2)}`;
    }
  });

  c.on("stats", (s) => {
    if (s.rttMs == null) {
      els.statRtt.textContent = "—";
      els.latencyBadge.textContent = "RTT —";
      els.latencyBadge.classList.remove("ok", "warn", "err");
    } else {
      const ms = Math.round(s.rttMs);
      els.statRtt.textContent = `${ms} ms`;
      els.statRtt.className = "v" + (ms < 150 ? " under-target" : ms < 300 ? " warn" : " error");
      els.latencyBadge.textContent = `RTT ${ms} ms`;
      els.latencyBadge.classList.toggle("ok", ms < 150);
      els.latencyBadge.classList.toggle("warn", ms >= 150 && ms < 300);
      els.latencyBadge.classList.toggle("err", ms >= 300);
    }
    els.statAudio.textContent = String(s.sentAudio);
    els.statVideo.textContent = `${s.sentVideo} / ${s.skippedVideo}`;
  });

  c.on("error", (e) => {
    setStatus(`${e.title}: ${e.message}`, "error");
  });

  c.on("disconnected", (e) => {
    if (!e.wasClean && Date.now() < sessionEndsAt) {
      setStatus(`Disconnected (${e.code}) ${e.reason || ""}`.trim(), "error");
    }
    stopSession({ silent: true });
  });
}

// --- Waveform ----------------------------------------------------------------
function startWaveform(stream) {
  try {
    meterCtx = new (window.AudioContext || window.webkitAudioContext)();
    const src = meterCtx.createMediaStreamSource(stream);
    meterAnalyser = meterCtx.createAnalyser();
    meterAnalyser.fftSize = 2048;
    meterAnalyser.smoothingTimeConstant = 0.3;
    src.connect(meterAnalyser);
  } catch (err) {
    console.warn("[cloud-live-demo] AudioContext failed:", err);
    return;
  }
  const cvs = els.waveform;
  const dpr = window.devicePixelRatio || 1;
  function resize() {
    const r = cvs.getBoundingClientRect();
    cvs.width = Math.max(1, Math.round(r.width * dpr));
    cvs.height = Math.max(1, Math.round(r.height * dpr));
  }
  resize();
  window.addEventListener("resize", resize, { passive: true });
  const ctx = cvs.getContext("2d");
  function draw() {
    meterAnalyser.getFloatTimeDomainData(meterBuf);
    const w = cvs.width, h = cvs.height;
    ctx.clearRect(0, 0, w, h);
    ctx.lineWidth = 2 * dpr;
    ctx.strokeStyle = "rgba(110, 168, 255, 0.95)";
    ctx.beginPath();
    const step = meterBuf.length / w;
    for (let x = 0; x < w; x++) {
      const v = meterBuf[Math.floor(x * step)] || 0;
      const y = h / 2 + v * (h / 2) * 0.95;
      if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    meterRaf = requestAnimationFrame(draw);
  }
  meterRaf = requestAnimationFrame(draw);
}
function stopWaveform() {
  if (meterRaf) cancelAnimationFrame(meterRaf);
  meterRaf = 0;
  if (meterCtx) { meterCtx.close().catch(() => {}); meterCtx = null; }
  meterAnalyser = null;
  const ctx = els.waveform.getContext("2d");
  ctx && ctx.clearRect(0, 0, els.waveform.width, els.waveform.height);
}

// --- Countdown ---------------------------------------------------------------
function startCountdown(endsAt) {
  sessionEndsAt = endsAt;
  function tick() {
    const remaining = Math.max(0, Math.ceil((endsAt - Date.now()) / 1000));
    els.countdown.textContent = `${remaining}s`;
    els.countdown.classList.toggle("tick", remaining <= 10 && remaining > 0);
    if (remaining <= 0) {
      stopSession({ reason: "Session ended (60-second demo limit). Get a token to keep going." });
    }
  }
  tick();
  countdownTimer = setInterval(tick, 250);
}
function stopCountdown() {
  if (countdownTimer) clearInterval(countdownTimer);
  countdownTimer = null;
  els.countdown.classList.remove("tick");
  els.countdown.textContent = `${CONFIG.sessionMaxSec}s`;
}

// --- Lifecycle ---------------------------------------------------------------
async function startSession() {
  els.startBtn.disabled = true;
  setStatus("Requesting a 60-second demo token…");
  const tok = await fetchDemoToken();
  if (!tok.ok) {
    showComingSoon(tok.reason);
    return;
  }
  setStatus("Got demo token. Asking for mic + camera…");
  predCount = 0;
  els.statPreds.textContent = "0";

  try {
    client = new AttentionClient({
      token: tok.token,
      url: tok.wsUrl,
      initialThreshold: CONFIG.threshold,
    });
    wireClient(client);
    setRunning(true);
    await client.start({ videoElement: els.video });
    const stream = els.video.srcObject;
    if (stream) startWaveform(stream);
    if (sessionTimer) clearTimeout(sessionTimer);
    const endsAt = Date.now() + tok.expiresInSec * 1000;
    sessionTimer = setTimeout(() => stopSession({ reason: "Session ended (60-second demo limit). Get a token to keep going." }), tok.expiresInSec * 1000);
    startCountdown(endsAt);
  } catch (err) {
    setStatus(`Couldn't start: ${err.message || err}`, "error");
    setRunning(false);
    showStartGate();
  }
}

function stopSession({ reason = "Stopped.", silent = false } = {}) {
  if (sessionTimer) { clearTimeout(sessionTimer); sessionTimer = null; }
  stopCountdown();
  stopWaveform();
  if (client) {
    try { client.stop?.(); } catch (_) { /* ignore */ }
    client = null;
  }
  setRunning(false);
  if (!silent) setStatus(reason);
  showStartGate();
}

// --- Boot --------------------------------------------------------------------
async function boot() {
  // Pre-flight: prove the browser has what we need before we ask the user.
  const missing = [];
  if (!navigator.mediaDevices?.getUserMedia) missing.push("getUserMedia");
  if (!window.AudioContext && !window.webkitAudioContext) missing.push("AudioContext");
  if (!window.WebSocket) missing.push("WebSocket");
  if (missing.length) {
    showComingSoon(`Your browser is missing required features: ${missing.join(", ")}. Try the latest Chrome, Edge, Firefox, or Safari.`);
    return;
  }
  // Secure context required for getUserMedia
  if (!window.isSecureContext && location.hostname !== "localhost" && location.hostname !== "127.0.0.1") {
    showComingSoon("This page must be served over HTTPS for mic + camera permission.");
    return;
  }
  try {
    await loadSDK();
  } catch (_) {
    return; // showComingSoon already called
  }
  showStartGate();
  els.startBtn.addEventListener("click", startSession);
  els.stopBtn.addEventListener("click", () => stopSession({ reason: "Stopped." }));
}

boot();
