<p align="center">
  <a href="../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# Examples

Real SDK-integration code for the voice-agent stacks SAA supports. Each adapter is a working drop-in: clone the repo, fill `.env`, run `make dev`, hear gated audio inside five minutes.

All examples consume the public cloud SDK ([`@attenlabs/saa-js`](https://www.npmjs.com/package/@attenlabs/saa-js) on npm, [`attenlabs-saa`](https://pypi.org/project/attenlabs-saa/) on PyPI). No example depends on a private model artifact.

## Framework adapters

| Adapter | What it shows | Works with | Offline smoke |
|---|---|---|---|
| [`openai-realtime/`](./openai-realtime) | Browser SAA + OpenAI Realtime: client-side gating, ephemeral `client_secret` mint, 16/24 kHz sample-rate matching, `response.cancel` barge-in, tool calls. | `openai-realtime` REST + WS on the v1 API | `make -C openai-realtime test-shape` |
| [`pipecat/`](./pipecat) | Drop-in `SAAGate(FrameProcessor)` placed before STT in the Pipecat 1.x pipeline; sidecar `SAA*Frame`s for observability; `InterruptionFrame` barge-in; Pipecat TTFB metrics. | `pipecat-ai >= 1.0` | `make -C pipecat test-shape` |
| [`livekit/`](./livekit) | Pre-STT gate via `Agent.stt_node` override; LiveKit camera frames forwarded to `AttentionClient.feed_video`; function-tool driven `set_threshold`/`mute`. | `livekit-agents >= 1.0` + `livekit-plugins-{openai,deepgram,silero}` | `make -C livekit test-shape` |
| [`twilio/`](./twilio) | PSTN bridge: Twilio Media Streams, mu-law 8 kHz to PCM16 16 kHz with a pure-NumPy codec, 20 ms paced playback, automatic `mark_responding(True)` on outbound bytes. Two reference bridges (OpenAI Realtime; Deepgram + OpenAI + ElevenLabs). | `twilio >= 9` + FastAPI | `make -C twilio test-shape` |
| [`elevenlabs-cai/`](./elevenlabs-cai) | Browser SAA + ElevenLabs CAI: server-minted WebRTC tokens, `setMicMuted` driven by SAA verdict, `sendContextualUpdate` from `speechReady`/face-count, three `clientTools` the agent can invoke. Also a `/twilio` WebSocket relay for PSTN. | `@elevenlabs/client` (WebRTC) + `@attenlabs/saa-js` | `make -C elevenlabs-cai test-shape` |
| [`proactive-agent/`](./proactive-agent) | **Make any voice agent proactive.** Multi-framework directory with one overlay per adapter: `twilio/` (AI-SDR outbound), `openai-realtime/` (laptop / coding / dashboard), `pipecat/` (meeting follow-up), `livekit/` (room-based), `elevenlabs-cai/` (browser WebRTC). | All five stacks above | `make -C proactive-agent/<framework> test-shape` |

Every adapter uses the public SDK semantic surface: `feed_audio` / `feed_video`, `mark_responding` / `markResponding`, `mute` / `unmute`, `set_threshold` / `setThreshold`. No private surfaces, no deprecated event names.

## Supporting examples

| Example | What it is |
|---|---|
| [`cloud-live-demo/`](./cloud-live-demo) | Canonical browser demo wiring `@attenlabs/saa-js` to a minimal UI. The simplest first thing to run. |
| [`production-gate/`](./production-gate) | Browser skeleton that pairs `@attenlabs/saa-js` with [`@attenlabs/saa-gate`](../packages/saa-gate) to route allowed speech into a downstream realtime agent. The reference shape for production routing. |
| [`obs-overlay/`](./obs-overlay) | Drop-in HTML overlay rendering SAA decisions (PASS · DROP · ABSTAIN · OVERRIDE) for OBS, dashboards, and demos. Example code, not a published npm package. Drop into any of the framework adapters for live observability of the gate. |

## Conventions

- `ATTENLABS_TOKEN` is the cloud auth token everywhere.
- `SAA_THRESHOLD` is the device-class confidence threshold (default `0.7`).
- Browser-fronted relays bind to port **8080**. Telephony bridges bind to **8000**.
- The `make test-shape` offline smokes are stdlib-only AST checks; they import nothing from the SDK and require no `attenlabs-saa` / `@attenlabs/saa-js` install. The deeper `make test` pytest suites (twilio, livekit, pipecat) each have a `tests/conftest.py` that puts the example dir on `sys.path`; the twilio and livekit suites additionally add `packages/saa-py/src` and stub `cv2` / `sounddevice` (the pipecat suite imports its own `saa_gate.py` and does not need the stubs). Those run locally, not in CI. Live cloud verification is manual.

## See also

- [`packages/saa-js/README.md`](../packages/saa-js/README.md) and [`packages/saa-py/README.md`](../packages/saa-py/README.md), the SDK reference.
- [`packages/saa-gate/`](../packages/saa-gate), the production routing policy state machine consumed by the framework adapters.
- [`packages/saa-proactive-js/`](../packages/saa-proactive-js) and [`packages/saa-proactive-py/`](../packages/saa-proactive-py), the lifecycle helpers used by every `proactive-agent/` overlay.
- [`examples/proactive-agent/`](./proactive-agent/): five proactive-overlay variants (Twilio, Pipecat, LiveKit, OpenAI Realtime, ElevenLabs CAI).
- The technical report ([arXiv:2604.08412](https://arxiv.org/abs/2604.08412)) for the headline benchmark numbers and architecture overview.


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
