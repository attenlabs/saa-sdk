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
  if (!ev.participant?.local) return;
  if (ev.track?.kind !== "video") return;
  const el = document.getElementById("local-video");
  el.srcObject = new MediaStream([ev.track]);
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
      setStatus("started");
      break;
    case "prediction":
      renderPrediction(data);
      break;
    case "vad":
      renderVAD(data);
      break;
    case "state":
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

function renderPrediction(p) {
  document.getElementById("class-label").textContent = LABELS[p.aligned_class] ?? "?";
  document.getElementById("conf-fill").style.width = `${(p.confidence * 100).toFixed(0)}%`;
  document.getElementById("faces").textContent = `faces: ${p.num_faces}`;
  document.getElementById("prediction").dataset.class = String(p.aligned_class);
}

function renderVAD(v) {
  document.getElementById("vad").textContent = `VAD: ${v.is_speech ? "on" : "off"}`;
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
  setStatus("disconnected");
  document.getElementById("btn-start").disabled = false;
  document.getElementById("btn-stop").disabled = true;
}
