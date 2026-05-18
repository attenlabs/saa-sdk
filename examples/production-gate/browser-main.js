import { AttentionClient } from "@attenlabs/saa-js";
import { createSaaGate } from "@attenlabs/saa-gate";
import { createOpenAIRealtimeSaaRouter } from "./openai-realtime-bridge.js";

/**
 * Browser-side production skeleton.
 *
 * This file is intentionally written as direct application code, not a demo
 * abstraction. Replace createRealtimeDataChannel() with your own Realtime,
 * LiveKit, Pipecat, Twilio, or STT bridge.
 */

const videoElement = document.querySelector("video#preview");
const statusElement = document.querySelector("#status");
const metricsElement = document.querySelector("#metrics");

async function main() {
  const session = await mintSessionToken();
  const realtime = await createRealtimeDataChannel();
  const realtimeRouter = createOpenAIRealtimeSaaRouter({
    dataChannel: realtime.dataChannel,
    response: {
      modalities: ["audio", "text"],
    },
  });

  const saa = new AttentionClient({
    url: session.ws_url,
    token: session.token,
    // Illustrative threshold for demo purposes; production code should source from runtime config or your own deployment policy.
    initialThreshold: 0.5,
    reconnect: { enabled: true, maxAttempts: 8 },
  });

  const gate = createSaaGate({
    profile: "desktop",
    onAllowSpeech: async ({ speech }) => {
      setStatus("addressed speech routed");
      realtimeRouter.routeSpeechReady(speech);
    },
    onDropSpeech: (decision) => {
      setStatus(`speech dropped: ${decision.reason}`);
    },
    onMetric: renderMetric,
  });

  gate.attach(saa);

  // Mark assistant audio playback so SAA does not feed speaker echo back into
  // the next agent turn. Call this around whichever audio player your stack uses.
  realtime.onAssistantAudioStart = () => gate.markResponding(true);
  realtime.onAssistantAudioEnd = () => gate.markResponding(false);

  await saa.start({ videoElement });
  await saa.ready();
  setStatus("ready");

  window.saa = saa;
  window.saaGate = gate;
}

async function mintSessionToken() {
  const response = await fetch("/v1/saa/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ttl_seconds: 60, scope: "browser" }),
  });
  if (!response.ok) throw new Error(`session token mint failed: ${response.status}`);
  return response.json();
}

async function createRealtimeDataChannel() {
  // Replace this stub with your app's existing Realtime connection.
  // For OpenAI Realtime over WebRTC, create an RTCPeerConnection, open a
  // data channel for events, and configure the session for manual audio input.
  const sent = [];
  return {
    dataChannel: {
      send(message) {
        sent.push(JSON.parse(message));
        console.debug("realtime event", message);
      },
    },
    onAssistantAudioStart: null,
    onAssistantAudioEnd: null,
    sent,
  };
}

function setStatus(text) {
  if (statusElement) statusElement.textContent = text;
}

function renderMetric(metric) {
  if (!metricsElement) return;
  metricsElement.textContent = JSON.stringify(metric, null, 2);
}

main().catch((error) => {
  console.error(error);
  setStatus(error.message);
});
