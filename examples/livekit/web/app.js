// minimal LiveKit + SAA browser client, no build step
// connects to a room, publishes cam+mic, plays the agent's audio, and renders
// SAA's prediction stream as an overlay
import { parseTurnPayload } from "./turn-parser.js";

const { Room, RoomEvent, Track, createLocalTracks } = LivekitClient;

const TOKEN_ENDPOINT = "/token";
const SAA_TOPIC = "saa";
const MAX_TURN_BYTES = 16 * 1024 * 1024; // sanity cap on a single turn payload

let room = null;

document.getElementById("btn-start").onclick = start;
document.getElementById("btn-stop").onclick = stop;

async function start() {
  document.getElementById("btn-start").disabled = true;
  setStatus("connecting…");
  try {
    const roomName = `saa-demo-${Date.now()}`;
    const identity = `user-${Math.random().toString(36).slice(2, 8)}`;

    // fetch a browser join token (SAA is summoned by the voice agent in the room)
    const resp = await fetch(`${TOKEN_ENDPOINT}?room=${roomName}&identity=${identity}`);
    if (!resp.ok) throw new Error(`token server returned ${resp.status}`);
    const { url, token } = await resp.json();

    room = new Room({ adaptiveStream: true, dynacast: true });
    room.on(RoomEvent.DataReceived, onData);
    room.registerByteStreamHandler(SAA_TOPIC, onByteStream);
    // play the voice agent's audio when it joins (SAA's hidden agent is data-only,
    // so the only audio track in the room is the agent's TTS / realtime voice)
    room.on(RoomEvent.TrackSubscribed, (track) => {
      if (track.kind === Track.Kind.Audio) {
        const el = track.attach();
        el.autoplay = true;
        document.body.appendChild(el);
        setStatus("agent connected");
      }
    });
    room.on(RoomEvent.Disconnected, () => setStatus("disconnected"));

    await room.connect(url, token);
    setStatus("waiting for agent…");
    // SAA warms its model server-side once summoned; show that on the card
    // until the native `started` pivot lands.
    setWarming(true);

    // publish cam + mic (SAA is multimodal — it wants both)
    let tracks;
    try {
      tracks = await createLocalTracks({
        audio: true,
        video: { resolution: { width: 1280, height: 720 } },
      });
    } catch (e) {
      throw new Error(mediaErrorMessage(e));
    }
    for (const t of tracks) {
      await room.localParticipant.publishTrack(t);
      if (t.kind === Track.Kind.Video) {
        t.attach(document.getElementById("local-video"));
      }
    }

    document.getElementById("btn-stop").disabled = false;
  } catch (e) {
    console.error("[saa] start failed:", e);
    setStatus(`error: ${e.message || e}`, true);
    setWarming(false);
    if (room) {
      try { await room.disconnect(); } catch (_) {}
      room = null;
    }
    document.getElementById("btn-start").disabled = false;
  }
}

function mediaErrorMessage(e) {
  const name = (e && (e.name || e.code)) || "";
  switch (name) {
    case "NotAllowedError":
    case "PermissionDeniedError":
      return "camera/mic permission denied — allow access and click Start again";
    case "NotFoundError":
    case "DevicesNotFoundError":
      return "no camera/mic found — connect a device and retry";
    case "NotReadableError":
    case "TrackStartError":
      return "camera/mic is in use by another app — close it and retry";
    case "OverconstrainedError":
    case "ConstraintNotSatisfiedError":
      return "requested camera resolution unsupported on this device";
    default:
      return `camera/mic error: ${(e && e.message) || name || e}`;
  }
}

function onData(payload, _participant, _kind, topic) {
  // hidden SAA sender — the participant may be null; trust the topic scope
  if (topic !== SAA_TOPIC) return;

  let msg;
  try {
    msg = JSON.parse(new TextDecoder().decode(payload));
  } catch (e) {
    console.warn("[saa] dropping non-JSON message on saa topic:", e.message || e);
    return;
  }
  switch (msg.type) {
    // `started` = model loaded, keep "warming up"
    // until warmup_complete
    case "started": setStatus("warming up…"); break;
    // native pivot: server signals warmup is complete
    case "warmup_complete": setWarming(false); setStatus("live"); break;
    case "prediction": renderPrediction(msg); break;
    case "vad": renderVAD(msg); break;
    case "state": setStatus(msg.state); break;
    case "interrupt": console.log("[saa] interrupt", msg); break;
    case "interjection": console.log("[saa] interjection", msg); break;
    case "config": console.log("[saa] threshold", msg.model_class2_threshold); break;
  }
}

async function onByteStream(reader, _participantInfo) {
  // binary turn payload (PCM16 + optional JPEGs). The demo just logs its size;
  // wrap parsing so a malformed/partial payload can't crash the stream handler.
  try {
    const chunks = [];
    let total = 0;
    for await (const chunk of reader) {
      total += chunk.length;
      if (total > MAX_TURN_BYTES) {
        console.warn("[saa] turn payload exceeded cap, dropping");
        return;
      }
      chunks.push(chunk);
    }
    const buf = new Uint8Array(total);
    let o = 0;
    for (const c of chunks) {
      buf.set(c, o);
      o += c.length;
    }
    const { pcm16, frames } = parseTurnPayload(buf);
    console.log("[saa] turn payload", pcm16.length, "samples,", frames.length, "frames");
  } catch (e) {
    // most likely a misframed/partial payload
    console.warn("[saa] dropping malformed turn payload:", e.message || e);
  }
}

const LABELS = { 0: "silent", 1: "human ↔ human", 2: "talking to me" };

// rolling client-side log of recent predictions, newest first
const predBuffer = [];
const PRED_BUFFER_MAX = 12;

// Show a "warming up" state on the prediction card until the server's native
// `started` pivot — otherwise the card sits at "silent" through the
// multi-second model warmup.
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
  el.dataset.class = String(cls);
  el.dataset.responding = String(responding);
  // during AI playback the class is gated to silent — surface "responding"
  document.getElementById("class-label").textContent =
    responding ? "responding" : (LABELS[cls] ?? "?");
  const confPct = Math.round((p.confidence ?? 0) * 100);
  document.getElementById("conf-fill").style.width = `${confPct}%`;
  document.getElementById("conf-num").textContent = `${confPct}%`;
  document.getElementById("faces").textContent = `faces: ${p.num_faces}`;
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
}

function setStatus(s, isError = false) {
  const el = document.getElementById("status");
  el.textContent = s;
  el.classList.toggle("error", isError);
}

async function stop() {
  if (room) {
    try { await room.disconnect(); } catch (_) {}
  }
  room = null;
  setStatus("disconnected");
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
  document.getElementById("btn-start").disabled = false;
  document.getElementById("btn-stop").disabled = true;
}
