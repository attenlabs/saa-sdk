// minimal Daily + SAA browser client, no build step
// joins a Daily room, publishes cam+mic, renders SAA's prediction stream
// pulled from the "saa" app-message topic the hosted bot publishes on
import { parseTurnPayload } from "./turn-parser.js";

const SESSION_ENDPOINT = "/session";
const SAA_TOPIC = "saa";

// cap pending reassembly buffers so a buggy/hostile producer can't OOM us
const MAX_PENDING_STREAMS = 10;

let call = null;
let agentIdentity = null;
let agentSessionId = null;

// stream_id → { envelope, chunks: Uint8Array[], gathered, kind }
const pending = new Map();

document.getElementById("btn-start").onclick = start;
document.getElementById("btn-stop").onclick = stop;

async function start() {
  // f a previous start() failed mid-flight, the old call object
  // may still be alive and daily-js refuses a second createCallObject().
  // Destroy any leftover before creating a fresh one.
  if (call) {
    try { await call.leave(); } catch (_) {}
    try { call.destroy(); } catch (_) {}
    call = null;
  }

  setStatus("requesting session");
  document.getElementById("btn-start").disabled = true;

  // fetch a Daily room + user token + summon the SAA bot for this session
  let payload;
  try {
    const resp = await fetch(SESSION_ENDPOINT);
    if (!resp.ok) {
      const body = await resp.text();
      throw new Error(`/session ${resp.status}: ${body}`);
    }
    payload = await resp.json();
    console.log("[saa] /session ok", payload);
  } catch (e) {
    setStatus("error: /session failed");
    showError(
      `Could not start a session: ${e.message}\n\n` +
      `Check that token_server.py is running and DAILY_API_KEY + SAA_API_KEY ` +
      `are set in .env.`,
    );
    document.getElementById("btn-start").disabled = false;
    return;
  }
  const { room_url, user_token, agent_identity, voice_agent_enabled, voice_agent_missing_env } = payload;
  
  console.log("[saa] voice agent enabled?", voice_agent_enabled, "missing env?", voice_agent_missing_env);

  if (!room_url || !user_token) {
    setStatus("error: bad /session response");
    showError(
      `/session returned an unusable response: ${JSON.stringify(payload)}\n\n` +
      `Expected {room_url, user_token, agent_identity}.`,
    );
    document.getElementById("btn-start").disabled = false;
    return;
  }
  agentIdentity = agent_identity;
  agentSessionId = null;
  console.log(
    "[saa] connecting", { room_url, agent_identity, voice_agent_enabled },
  );

  // Surface whether the voice agent is enabled this session saves a
  // tester from wondering why nothing's talking back.
  if (voice_agent_enabled) {
    setMode("with voice agent");
  } else {
    setMode(
      "overlay only" +
      (voice_agent_missing_env?.length
        ? `- missing ${voice_agent_missing_env.join(", ")}`
        : ""),
    );
  }

  // call-object mode (no iframe). subscribeToTracksAutomatically lets us
  // receive any other participant's media (also voice agent) without
  // manually managing subscriptions.
  call = window.DailyIframe.createCallObject({
    subscribeToTracksAutomatically: true,
  });

  call.on("app-message", onAppMessage);
  call.on("track-started", onTrackStarted);
  call.on("track-stopped", onTrackStopped);
  call.on("participant-joined", onParticipantJoined);
  call.on("participant-updated", onParticipantUpdated);
  call.on("left-meeting", () => setStatus("disconnected"));

  try {
    // start both tracks
    await call.join({
      url: room_url,
      token: user_token,
      startVideoOff: false,
      startAudioOff: false,
    });
  } catch (e) {
    setStatus("error: join failed");
    showError(`Daily join() failed: ${e.message}`);
    try { call.destroy(); } catch (_) {}
    call = null;
    document.getElementById("btn-start").disabled = false;
    return;
  }
  setStatus("connected");
  clearError();
  // SAA warms its model once summoned; show that on the card until the first
  // real prediction lands.
  setWarming(true);
  console.log("[saa] joined room, waiting for bot on topic:", SAA_TOPIC);

  // resolve agent session_id in case it's already in the room
  resolveAgentSessionId();

  document.getElementById("btn-stop").disabled = false;
}

function showError(msg) {
  let el = document.getElementById("error-banner");
  if (!el) {
    el = document.createElement("pre");
    el.id = "error-banner";
    el.style.cssText =
      "white-space: pre-wrap; background: #2a0e0e; color: #ffb4b4; " +
      "padding: 12px; border-radius: 8px; margin: 12px 0; font-size: 12px;";
    document.getElementById("root").insertBefore(
      el, document.querySelector(".controls"),
    );
  }
  el.textContent = msg;
}

function clearError() {
  const el = document.getElementById("error-banner");
  if (el) el.remove();
}

function onTrackStarted(ev) {
  // Local video → self-preview.
  if (ev.participant?.local) {
    if (ev.track?.kind === "video") {
      document.getElementById("local-video").srcObject = new MediaStream([ev.track]);
    }
    return;
  }
  // Remote audio Daily call-object mode does NOT auto-play
  // attach the track to an <audio autoplay> element
  if (ev.track?.kind === "audio") {
    attachRemoteAudio(ev.participant?.session_id, ev.track);
  }
}

function attachRemoteAudio(sid, track) {
  const id = `remote-audio-${sid || "unknown"}`;
  let el = document.getElementById(id);
  if (!el) {
    el = document.createElement("audio");
    el.id = id;
    el.autoplay = true;
    el.playsInline = true;
    document.body.appendChild(el);
  }
  el.srcObject = new MediaStream([track]);
  const p = el.play?.();
  if (p?.catch) p.catch((e) => console.warn("[saa] remote audio play() blocked:", e.message));
  console.log("[saa] attached remote audio from", sid);
}

function onTrackStopped(ev) {
  if (ev.track?.kind !== "audio") return;
  const el = document.getElementById(`remote-audio-${ev.participant?.session_id || "unknown"}`);
  if (el) el.remove();
}

function onParticipantJoined(_ev) {
  resolveAgentSessionId();
}

function onParticipantUpdated(_ev) {
  if (agentSessionId === null) resolveAgentSessionId();
}

function resolveAgentSessionId() {
  // agent_identity from the server is the bot's Daily `userName`. Match it
  // against participants() to learn the bot's session id, which is what
  // `fromId` carries on the app-message event.
  if (!call || !agentIdentity) return;
  const parts = call.participants();
  for (const sid of Object.keys(parts)) {
    const p = parts[sid];
    if (p?.local) continue;
    if (p?.user_name === agentIdentity) {
      agentSessionId = p.session_id ?? sid;
      console.log("[saa] bot resolved, session_id:", agentSessionId);
      return;
    }
  }
}

function onAppMessage({ data, fromId }) {
  // Daily wraps app messages in { data, fromId }; the SAA bot tags its
  // payloads with { topic: "saa", type: ... }
  if (data?.topic !== SAA_TOPIC) return;
  // scope to the hosted bot's session id if we've resolved it; before that
  // we accept any sender so we don't lose the early `started` envelope
  if (agentSessionId && fromId && fromId !== agentSessionId) return;

  switch (data.type) {
    case "started":
      // `started` = model loaded, keep  "warming up"
      // until warmup_complete (first real prediction)
      console.log("[saa] model loaded", data);
      setStatus("warming up…");
      break;
    case "warmup_complete":
      // native pivot: server signals warmup is complete
      setWarming(false);
      setStatus("live");
      break;
    case "prediction":
      renderPrediction(data);
      break;
    case "vad":
      renderVAD(data);
      break;
    case "state":
      console.log("[saa] state:", data.state);
      setStatus(data.state);
      break;
    case "turn_ready":
      beginAssembly(data, "turn");
      break;
    case "interjection":
      beginAssembly(data, "interjection");
      break;
    case "turn_chunk":
      appendChunk(data);
      break;
    case "interrupt":
      console.log("[saa] interrupt", data);
      break;
    case "config":
      console.log("[saa] threshold", data.model_class2_threshold);
      break;
    case "error":
      console.warn("[saa] error", data.message);
      break;
  }
}

function beginAssembly(envelope, kind) {
  // turn_ready / interjection envelopes carry stream_id + total_chunks +
  // byte_len. The actual binary payload arrives in subsequent turn_chunk
  // messages keyed on stream_id.
  const sid = envelope.stream_id;
  if (!sid) return;
  if (pending.size >= MAX_PENDING_STREAMS) {
    const oldest = pending.keys().next().value;
    console.warn(
      "[saa] pending reassembly cap reached, dropping",
      oldest,
      "to make room for",
      sid,
    );
    pending.delete(oldest);
  }
  pending.set(sid, {
    envelope,
    kind,
    chunks: new Array(envelope.total_chunks),
    gathered: 0,
  });
}

function appendChunk(msg) {
  const p = pending.get(msg.stream_id);
  if (!p) return;
  if (p.chunks[msg.index] !== undefined) return;
  p.chunks[msg.index] = b64ToBytes(msg.data_base64);
  p.gathered += 1;
  if (p.gathered < p.envelope.total_chunks) return;

  // all chunks in concat into one buffer and decode
  let total = 0;
  for (const c of p.chunks) total += c.length;
  const buf = new Uint8Array(total);
  let o = 0;
  for (const c of p.chunks) {
    buf.set(c, o);
    o += c.length;
  }
  pending.delete(msg.stream_id);

  let parsed;
  try {
    parsed = parseTurnPayload(buf);
  } catch (e) {
    console.warn("[saa] turn payload decode failed:", e);
    return;
  }
  console.log(
    `[saa] ${p.kind} payload`,
    parsed.pcm16.length,
    "samples,",
    parsed.frames.length,
    "frames",
    p.envelope.context ? `(context=${p.envelope.context})` : "",
  );
}

function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

const LABELS = { 0: "silent", 1: "human ↔ human", 2: "talking to me" };

// rolling client-side log of recent predictions, newest first
const predBuffer = [];
const PRED_BUFFER_MAX = 12;

// log only on transitions — predictions/VAD arrive every 250ms
let _lastClass = null;
let _lastResponding = null;
let _lastVad = null;

// show "warming up" state on the prediction card until inference is actually
// live (first prediction)
function setWarming(on) {
  const el = document.getElementById("prediction");
  el.dataset.warming = String(on);
  if (on) {
    el.dataset.class = "0";
    el.dataset.responding = "false";
    document.getElementById("class-label").textContent = "warming up";
    document.getElementById("conf-fill").style.width = ""; // let the CSS sweep show
    document.getElementById("conf-num").textContent = "—";
  } else {
    document.getElementById("class-label").textContent = "—";
    document.getElementById("conf-fill").style.width = "0%";
    document.getElementById("conf-num").textContent = "0%";
  }
}

function renderPrediction(p) {
  const el = document.getElementById("prediction");
  // warming is cleared only by the warmup_complete message, not here
  const warming = el.dataset.warming === "true";
  // While warming, keep the "warming up" card and ignore the conf-0
  // buffer-fill predictions until warmup_complete fires.
  if (warming) return;
  // prefer the canonical polished display_class; fall back to class
  const cls = p.display_class ?? p.class;
  // native AI-responding flag; older servers signal it via source instead
  const responding = p.responding ?? p.source === "ai_responding";
  // during AI playback the class is gated to silent — surface "responding"
  const label = responding ? "responding" : (LABELS[cls] ?? "?");
  document.getElementById("class-label").textContent = label;
  const confPct = Math.round((p.confidence ?? 0) * 100);
  document.getElementById("conf-fill").style.width = `${confPct}%`;
  document.getElementById("conf-num").textContent = `${confPct}%`;
  document.getElementById("faces").textContent = `faces: ${p.num_faces}`;
  el.dataset.class = String(cls);
  el.dataset.responding = String(responding);
  if (cls !== _lastClass || responding !== _lastResponding) {
    console.log(
      `[saa] prediction → ${responding ? "responding" : cls} (${label})`,
      `conf=${p.confidence?.toFixed(2)} faces=${p.num_faces}`,
    );
    _lastClass = cls;
    _lastResponding = responding;
  }
  pushPredBuffer(p);
}

function pushPredBuffer(p) {
  const cls = p.display_class ?? p.class;
  const responding = p.responding ?? p.source === "ai_responding";
  predBuffer.unshift({
    cls,
    raw: p.class,
    conf: p.confidence ?? 0,
    faces: p.num_faces,
    responding,
  });
  predBuffer.length = Math.min(predBuffer.length, PRED_BUFFER_MAX);
  renderPredBuffer();
}

function renderPredBuffer() {
  const ul = document.getElementById("pred-buffer");
  if (!ul) return;
  ul.innerHTML = predBuffer
    .map((r) => {
      const label = r.responding ? "responding" : (LABELS[r.cls] ?? "?");
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

function renderVAD(v) {
  document.getElementById("vad").textContent = `VAD: ${v.is_speech ? "on" : "off"}`;
  if (v.is_speech !== _lastVad) {
    console.log(`[saa] vad → ${v.is_speech ? "speech" : "silence"}`);
    _lastVad = v.is_speech;
  }
}

function setStatus(s) {
  document.getElementById("status").textContent = s;
}

function setMode(s) {
  let el = document.getElementById("mode");
  if (!el) {
    el = document.createElement("span");
    el.id = "mode";
    el.className = "status";
    el.style.marginLeft = "8px";
    document.getElementById("status").after(el);
  }
  el.textContent = s;
}

async function stop() {
  if (call) {
    try {
      await call.leave();
    } finally {
      call.destroy();
    }
  }
  call = null;
  agentIdentity = null;
  agentSessionId = null;
  pending.clear();
  _lastClass = null;
  _lastResponding = null;
  _lastVad = null;
  // reset the prediction card to its idle look
  const pred = document.getElementById("prediction");
  pred.dataset.warming = "false";
  pred.dataset.responding = "false";
  pred.dataset.class = "0";
  document.getElementById("class-label").textContent = "--";
  document.getElementById("conf-fill").style.width = "0%";
  document.getElementById("conf-num").textContent = "0%";
  predBuffer.length = 0;
  renderPredBuffer();
  // tear down any remote-audio elements we created for the voice agent
  document.querySelectorAll("audio[id^='remote-audio-']").forEach((el) => el.remove());
  console.log("[saa] disconnected");
  setStatus("disconnected");
  document.getElementById("btn-start").disabled = false;
  document.getElementById("btn-stop").disabled = true;
}
