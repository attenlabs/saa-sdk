# Changelog

Notable changes to the SAA SDKs and helper packages in this repository. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); each package is versioned independently.

Published registries:

- [`@attenlabs/saa-js`](https://www.npmjs.com/package/@attenlabs/saa-js) on npm
- [`attenlabs-saa`](https://pypi.org/project/attenlabs-saa/) on PyPI

## Unreleased

See per-package release notes on npm / PyPI for version-pinned changes.

## Cloud SDKs — 0.3.0 (2026-05-14)

### `@attenlabs/saa-js@0.3.0`

- WebSocket SDK for the SAA cloud at `server.attentionlabs.ai`.
- Emits typed events: `prediction`, `vad`, `state`, `speechReady`, `config`, `stats`, `error`, `disconnected`.
- Methods: `start`, `stop`, `mute`, `unmute`, `markResponding`, `setThreshold`, `on`.
- Audio captured at 16 kHz PCM16; video captured as JPEG at 4 fps (configurable).
- Audio-only mode: omit `videoElement` on `start`.

### `attenlabs-saa@0.3.0`

- Python equivalent of `@attenlabs/saa-js`.
- Same event surface, same WebSocket protocol, same operating thresholds.
- Decorator-based handlers: `@client.on_speech_ready`, `@client.on_prediction`, etc.
- Configurable mic and camera; `enable_video=False` for audio-only deployments.

## Helper packages — 0.1.0 (initial release)

### `@attenlabs/saa-gate@0.1.0`

- Production routing-policy state machine over the cloud SDK's event stream.
- Profiles: `desktop`, `kiosk`, `robot`, `telephony`. Fail-closed defaults: privacy mute, agent-speaking with echo tail, transport-health (RTT + buffer thresholds).
- Emits structured allow/drop decisions with reason codes and audit history.
- Peer-dependency on `@attenlabs/saa-js >=0.2.0 <2`.

### `@attenlabs/saa-proactive@0.1.0` / `attenlabs-saa-proactive@0.1.0`

- Lifecycle helper for proactive voice agents: wraps `markResponding(true) → speak → tail → markResponding(false)` around any `speak` callback.
- `TriggerHub` in-process pub/sub for relaying `POST /trigger` webhooks to connected browsers via Server-Sent Events.
- Zero runtime deps (Python: FastAPI is an optional extra for the convenience router).
- Peer-dependency on the matching language's cloud SDK.

## Framework adapters

Drop-in integrations live under [`examples/`](./examples/) and ship with `Dockerfile`, `Makefile`, smoke tests, and a per-stack README:

- `examples/twilio/` — Twilio Media Streams (μ-law 8 kHz ↔ PCM16 16 kHz, three downstream-bridge options).
- `examples/pipecat/` — `SAAGate(FrameProcessor)` for any Pipecat 1.x pipeline.
- `examples/livekit/` — LiveKit Agents worker with pre-STT or response-mode gate.
- `examples/openai-realtime/` — Browser SAA + OpenAI Realtime relay with ephemeral tokens.
- `examples/elevenlabs-cai/` — Browser SAA + ElevenLabs CAI with server-minted WebRTC tokens.
- `examples/proactive-agent/` — Five proactive overlays (one per stack above).
- `examples/production-gate/`, `examples/cloud-live-demo/`, `examples/obs-overlay/` — supporting examples for production routing, the minimal browser demo, and the bounded decision flight-recorder.
