<p align="center">
  <a href="../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# @attenlabs/saa-gate

Production gate state machine for SAA integrations.

This package intentionally does not implement another WebSocket protocol. It wires the existing SAA client event stream into a deterministic routing policy:

```text
mic/camera -> SAA Cloud -> speechReady -> SAA gate -> STT/LLM/TTS
                                  \-> drop/audit
```

Use it when your product needs to make the downstream decision explicit: only addressed speech is forwarded to transcription, the LLM, tools, or TTS.

## Install in this repo

```bash
npm run test --workspace=@attenlabs/saa-gate
```

The test suite includes:

- **Unit tests** in [`tests/gate.test.mjs`](./tests/gate.test.mjs) covering the routing-policy state machine in isolation.
- **Integration tests** in [`tests/integration-saa-js.test.mjs`](./tests/integration-saa-js.test.mjs) that instantiate the real `@attenlabs/saa-js` `AttentionClient` (not a mock), attach the gate, fire the canonical event stream through the SDK's actual emitter, and verify allow/drop decisions, catching any drift between the SDK's public event surface and the gate's subscriptions.

### Live end-to-end

For a real cloud-backed run (with a token, mic, and camera), see [`examples/cloud-live-demo/`](../../examples/cloud-live-demo/) and [`examples/production-gate/`](../../examples/production-gate/). The latter wires the gate against `@attenlabs/saa-js` end-to-end with a downstream OpenAI Realtime relay.

## Minimal usage

```js
import { AttentionClient } from "@attenlabs/saa-js";
import { createSaaGate } from "@attenlabs/saa-gate";

const client = new AttentionClient({ token: sessionToken });
const gate = createSaaGate({
  profile: "desktop",
  onAllowSpeech: async ({ speech }) => {
    await sendToYourStt(speech.audioBase64);
  },
  onDropSpeech: (decision) => {
    console.warn("dropped", decision.reason);
  },
});

const detach = gate.attach(client);
await client.start({ videoElement });

// During TTS playback:
await gate.withAgentSpeech(async () => {
  await playAssistantAudio();
});

detach();
```

## Policy defaults

The defaults are intentionally conservative for production:

- fail closed when SAA health is bad or unknown
- block during local privacy mute
- block while the agent is speaking plus an echo tail
- cap utterance duration
- require enough SAA evidence before routing speech
- emit a structured decision for every allowed or dropped utterance

## Decision shape

Every allow/drop emits a structured record:

```js
{
  action: "allow" | "drop",
  reason: "addressed" | "muted" | "agent-speaking" | "echo-tail" |
          "transport-unhealthy" | "no-addressed-evidence" |
          "speech-too-short" | "speech-too-long" | "no-audio" |
          "downstream-timeout" | "downstream-error",
  ts: "2026-05-15T01:02:03.456Z",
  seq: 42,
  profile: "desktop",
  durationSec: 1.2,
  lastObservedConfidence: 0.83, // last positive prediction score observed
                                // before this decision; NOT a confidence
                                // attached to this specific utterance
  auditOpen: true,
  connected: true,
  context: { sessionId?, traceId? }
}
```

`lastObservedConfidence` is intentionally named to be honest: in the default
`trustSpeechReady: true` mode the gate routes on the cloud's server-side
decision, so the confidence attached to the decision is the most recent
positive prediction observed in the evidence window, not a score derived from
this specific utterance.

## Helpers

Three small helpers are exported alongside the gate. None is required to use it; they exist because the framework adapters and the production-gate skeleton needed each one, and exporting is cheaper than copy/paste.

### `forwardSpeechReadyToOpenAIRealtime(dataChannel, speech, options?)`

Sends a `speechReady` payload into an open OpenAI Realtime data channel as `input_audio_buffer.append` + `input_audio_buffer.commit` + (optionally) `response.create`. Validates that `dataChannel` exposes `send(string)`, that `speech.audioBase64` is present, that `speech.encoding === "pcm16"`, and that `speech.sampleRate === 16000`, throwing `TypeError` rather than silently sending the wrong shape. Use it when your Realtime session is configured for manual audio-buffer control (no automatic VAD, no automatic response creation).

```js
import { forwardSpeechReadyToOpenAIRealtime } from "@attenlabs/saa-gate";

forwardSpeechReadyToOpenAIRealtime(dataChannel, speech, {
  eventIdPrefix: "saa-utt-42",
  response: { modalities: ["audio", "text"] },
  createResponse: true, // default true; set false to commit without firing response.create
});
```

Reference wire-up: [`examples/production-gate/openai-realtime-bridge.js`](../../examples/production-gate/openai-realtime-bridge.js).

### `pcm16ToBase64(pcm16)`

Converts an `Int16Array` of PCM16 audio to a base64 string. Useful when you need to serialise `speech.audioPcm16` (which the SDK provides as a typed array) over a JSON transport that doesn't accept binary. Works in both browsers (via `btoa`) and Node (falls back to `Buffer`).

```js
import { pcm16ToBase64 } from "@attenlabs/saa-gate";

const b64 = pcm16ToBase64(speech.audioPcm16);
```

### `positivePredictionScore(prediction)`

Returns the device-class (`cls === 2`) confidence from a `prediction` event, or `0` if the prediction is not device-directed. The gate uses this internally to advance its evidence window; consumers can compute the same score against an observed prediction stream without reaching into private gate state.

```js
import { positivePredictionScore } from "@attenlabs/saa-gate";

client.on("prediction", (p) => {
  const score = positivePredictionScore(p);
  if (score >= 0.7) flashGreen();
});
```

### Constants and error classes

The string literals in the Decision shape are exported as frozen objects so you can compare type-safely instead of string-matching:

```js
import { DECISION, REASON, PROFILES, DEFAULT_POLICY, SaaGateTimeoutError } from "@attenlabs/saa-gate";

// DECISION.ALLOW === "allow", DECISION.DROP === "drop"
if (decision.action === DECISION.ALLOW) { /* ... */ }

// REASON.ADDRESSED, REASON.MUTED, REASON.AGENT_SPEAKING, REASON.ECHO_TAIL,
// REASON.UNHEALTHY, REASON.NO_EVIDENCE, REASON.TOO_SHORT, REASON.TOO_LONG,
// REASON.NO_AUDIO, REASON.DOWNSTREAM_TIMEOUT, REASON.DOWNSTREAM_ERROR
if (decision.reason === REASON.AGENT_SPEAKING) { /* ... */ }

// PROFILES keys: "desktop" | "kiosk" | "robot" | "telephony"
const gate = createSaaGate({ profile: PROFILES.desktop.profile });

// DEFAULT_POLICY is the same frozen object PROFILES.desktop merges from.
console.log(DEFAULT_POLICY.echoTailMs); // 450
```

`SaaGateTimeoutError` is thrown by the gate's `onAllowSpeech` wrapper when a downstream callback exceeds `policy.downstreamTimeoutMs` (recorded as `decision.reason === REASON.DOWNSTREAM_TIMEOUT`). Catch it explicitly if you want to distinguish timeouts from other downstream errors:

```js
try {
  await someRoutedSpeechHandler(speech);
} catch (err) {
  if (err instanceof SaaGateTimeoutError) {
    metrics.timeout++;
  } else {
    metrics.error++;
  }
  throw err;
}
```

## See also

- [`packages/saa-js/README.md`](../saa-js/README.md): the cloud SDK this gate consumes.
- [`examples/production-gate/README.md`](../../examples/production-gate/README.md): the reference browser skeleton wiring `@attenlabs/saa-js` + this gate against a downstream realtime agent.
- [`examples/cloud-live-demo/README.md`](../../examples/cloud-live-demo/README.md): the minimal browser demo (no gate).
- [`examples/README.md`](../../examples/README.md): framework adapter index.

## Health thresholds

Transport health is policy-configurable:

```js
createSaaGate({
  policy: {
    unhealthyRttMs: 2000,         // RTT above this is unhealthy
    unhealthyBufferedAmount: 2_000_000, // backpressure above this is unhealthy
    maxStatsAgeMs: 30_000,        // stats older than this count as unknown
  },
});
```


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
