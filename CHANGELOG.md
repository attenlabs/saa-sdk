# Changelog

Notable changes to the SAA packages in this repository. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); each package is versioned independently.

Published registries:

- [`@attenlabs/saa-js`](https://www.npmjs.com/package/@attenlabs/saa-js) on npm
- [`attenlabs-saa`](https://pypi.org/project/attenlabs-saa/) on PyPI

## Unreleased

### `@attenlabs/saa-js` 0.4.0

- Native warmup signal: `warmupComplete` now fires on the server's `started` pivot (after the model warms up) instead of being inferred from the first non-zero-confidence prediction. The inference path is kept as a fallback for older servers.
- Native AI-responding state: `PredictionEvent.responding` reflects the server's per-tick flag, with `source === "ai_responding"` as the old-server fallback. Consumers no longer need to synthesize a "responding" state during AI playback.

### `attenlabs-saa` 0.5.0

- Parity with `@attenlabs/saa-js`: added the `interjection` event (`on_interjection` + `InterjectionEvent`) and `TurnReadyEvent.context` (e.g. `"interjection_follow_up"`), both previously missing.
- Native warmup signal (`warmup_complete` on `started`, conf>0 fallback) and native AI-responding state (`PredictionEvent.responding`), matching the JS SDK.

### `saa-livekit-client` 0.2.0 · `saa-pipecat-client` 0.2.0

- `PredictionEvent.responding` surfaces the server's native AI-responding flag (falls back to `source == "ai_responding"`).
- Standardized on the `SAA_API_KEY` environment variable across docstrings and quickstarts.

### Examples

- Added `python/` and `web/` streaming-SDK demos (subtree-merged from the standalone demo repos): the SDK driving its own capture loop end to end, with detected turns routed to OpenAI Realtime.
- The `livekit/web` and `pipecat/web` browser samples now render the native warmup and AI-responding states (the prediction card shows a distinct "responding" colour during AI playback instead of "silent").

## Streaming SDKs — 0.3.x

### `@attenlabs/saa-js`

- WebSocket streaming client for the SAA cloud.
- Emits typed events: `prediction`, `vad`, `state`, `turnReady`, `config`, `stats`, `interrupt`, `interjection`, `error`, `disconnected`.
- Methods: `start`, `stop`, `mute`, `unmute`, `markResponding`, `setThreshold`, `on` / `off`.
- Audio captured at 16 kHz PCM16; video captured as JPEG (configurable fps).
- Audio-only mode: omit `videoElement` on `start`.

### `attenlabs-saa`

- Python equivalent of `@attenlabs/saa-js`.
- Same WebSocket protocol and operating thresholds.
- Decorator-based handlers: `@client.on_turn_ready`, `@client.on_prediction`, `@client.on_vad`, etc.
- Configurable mic and camera; `enable_video=False` for audio-only deployments.

## LiveKit hosted bridge — `saa-livekit-client` 0.1.0

- Summons a hidden participant into the customer's LiveKit room that runs the classifier server-side and publishes events on the `"saa"` data topic.
- `AttentionEngine` exposes `on_prediction` / `on_vad` / `on_turn_ready` / `on_interrupt` / `on_interjection` callbacks and `mute` / `unmute` / `responding_start` / `responding_stop` / `set_threshold` actions.
- `start_attention_session`, `attention_agent_token`, and `build_attention_entrypoint` helpers.
- No ML dependencies; pure Python.

## Examples

- `examples/livekit/` — three runnable LiveKit Agents 1.5.x samples: `voice_agent_cascaded`, `voice_agent_realtime`, and `web`.

Adapters for other stacks (Twilio, Pipecat, OpenAI Realtime, ElevenLabs CAI) are on the roadmap, see [`README.md`](./README.md#roadmap).
