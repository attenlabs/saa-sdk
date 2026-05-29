// minimal LiveKit + SAA browser client, no build step
// connects to a room, publishes cam+mic, renders SAA's prediction stream
import { parseTurnPayload } from "./turn-parser.js";

const { Room, RoomEvent, Track, createLocalTracks } = LivekitClient;

const TOKEN_ENDPOINT = "/token";
const SAA_TOPIC = "saa";

let room = null;
let agentIdentity = null;

document.getElementById("btn-start").onclick = start;
document.getElementById("btn-stop").onclick = stop;

async function start() {
  const roomName = `saa-demo-${Date.now()}`;
  const identity = `user-${Math.random().toString(36).slice(2, 8)}`;

  // fetch a join token + summon the SAA agent for this room
  const resp = await fetch(`${TOKEN_ENDPOINT}?room=${roomName}&identity=${identity}`);
  const { url, token, agent_identity } = await resp.json();
  agentIdentity = agent_identity;

  room = new Room({ adaptiveStream: true, dynacast: true });
  room.on(RoomEvent.DataReceived, onData);
  room.registerByteStreamHandler(SAA_TOPIC, onByteStream);

  await room.connect(url, token);
  setStatus("connected");

  // publish cam + mic (SAA is multimodal — it wants both)
  const tracks = await createLocalTracks({
    audio: true,
    video: { resolution: { width: 1280, height: 720 } },
  });
  for (const t of tracks) {
    await room.localParticipant.publishTrack(t);
    if (t.kind === Track.Kind.Video) {
      t.attach(document.getElementById("local-video"));
    }
  }

  document.getElementById("btn-start").disabled = true;
  document.getElementById("btn-stop").disabled = false;
}

function onData(payload, participant, _kind, topic) {
  if (topic !== SAA_TOPIC) return;
  // hidden sender — participant may be null, trust the topic scope
  if (participant && agentIdentity && participant.identity !== agentIdentity) return;

  const msg = JSON.parse(new TextDecoder().decode(payload));
  switch (msg.type) {
    case "prediction":
      renderPrediction(msg);
      break;
    case "vad":
      renderVAD(msg);
      break;
    case "state":
      setStatus(msg.state);
      break;
    case "interrupt":
      console.log("[saa] interrupt", msg);
      break;
    case "interjection":
      console.log("[saa] interjection", msg);
      break;
    case "config":
      console.log("[saa] threshold", msg.model_class2_threshold);
      break;
  }
}

async function onByteStream(reader, participantInfo) {
  // binary turn payload (PCM16 + optional JPEGs) — decoded here if the demo
  // needs the audio; this demo just logs its size
  const chunks = [];
  for await (const chunk of reader) chunks.push(chunk);
  const total = chunks.reduce((a, c) => a + c.length, 0);
  const buf = new Uint8Array(total);
  let o = 0;
  for (const c of chunks) {
    buf.set(c, o);
    o += c.length;
  }
  const { pcm16, frames } = parseTurnPayload(buf);
  console.log("[saa] turn payload", pcm16.length, "samples,", frames.length, "frames");
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

async function stop() {
  if (room) await room.disconnect();
  room = null;
  setStatus("disconnected");
  document.getElementById("btn-start").disabled = false;
  document.getElementById("btn-stop").disabled = true;
}
