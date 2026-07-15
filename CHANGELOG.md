# Changelog

Notable changes to the SAA packages in this repository. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); each package is versioned independently.

Published registries:

- [`@attenlabs/saa-js`](https://www.npmjs.com/package/@attenlabs/saa-js) on npm
- [`attenlabs-saa`](https://pypi.org/project/attenlabs-saa/) on PyPI
- [`saa-livekit-client`](https://pypi.org/project/saa-livekit-client/) on PyPI
- [`saa-pipecat-client`](https://pypi.org/project/saa-pipecat-client/) on PyPI

## 2026-07-15

### `attenlabs-saa` 0.7.2 · `@attenlabs/saa-js` 0.7.2

- better camera error handling, switch to audio_only profile
- websocket resource lifecycle improvements, better guards

## 2026-07-10

### `saa-pipecat-client` 0.3.4

- Fix leaking SessionHandle and improper close

## 2026-06-25

### `attenlabs-saa` 0.7.1

- Cleaner shutdown path and README updates. No API changes.

### `saa-livekit-client` 0.3.3 · `saa-pipecat-client` 0.3.3

- Error handling on startup: surface a clear error if the hosted agent fails to start.

### `saa-livekit-client` 0.3.2 · `saa-pipecat-client` 0.3.2

- Better environment-variable loading/handling; proper startup error if the agent fails; livekit-client stashing improvements plus a cap on the pending-turns dict.

## 2026-06-24

### `@attenlabs/saa-js` 0.7.0

- Network resilience: auto-reconnect with typed `reconnecting` / `reconnected` events, plus client-side logging helpers.

## 2026-06-23

### `attenlabs-saa` 0.7.0

- Network resilience: `auto_reconnect` (default on) with `on_reconnecting` / `on_reconnected` events and full-jitter backoff reconnect; network/broker helpers.

## 2026-06-19

### `@attenlabs/saa-js` 0.6.1 · `attenlabs-saa` 0.6.1

- Documentation and copy cleanup: clearer package descriptions, removed internal-tooling and integration jargon, and corrected the documented `jpegQuality` / `jpeg_quality` defaults (0.5 / 50). No API changes.

### `saa-livekit-client` 0.3.1 · `saa-pipecat-client` 0.3.1

- Documentation and copy cleanup: clearer package descriptions, removed internal-tooling and integration jargon, and dropped the cascaded sample in favor of the realtime and web samples. No API changes.

### `@attenlabs/saa-js` 0.6.0

- Native warmup signal: `warmupComplete` fires on the server's dedicated `warmup_complete` message, sent once the model is warmed up and producing real predictions (replacing the old heuristic of inferring readiness from the first non-zero-confidence prediction).
- Native AI-responding state: `PredictionEvent.responding` reflects the server's per-tick flag, with `source === "ai_responding"` as the old-server fallback. Consumers no longer need to synthesize a "responding" state during AI playback.
- External-frame capture: `feedAudio` / `feedVideo` accept caller-supplied media, and `serverProfile` (auto-selected `audio_only` when video is disabled) lets the SDK gate stacks that own their own capture loop.

### `attenlabs-saa` 0.6.0

- Parity with `@attenlabs/saa-js`: added the `interjection` event (`on_interjection` + `InterjectionEvent`) and `TurnReadyEvent.context` (e.g. `"interjection_follow_up"`), both previously missing.
- Native warmup signal: `on_warmup_complete` fires on the server's dedicated `warmup_complete` message (replacing the old first-non-zero-confidence heuristic), plus native AI-responding state (`PredictionEvent.responding`), matching the JS SDK.
- External-frame capture: `feed_audio` / `feed_video` accept caller-supplied media, and `server_profile` (auto-selected `audio_only` when `enable_video=False`) lets the SDK gate stacks that own their own capture loop.

### `saa-livekit-client` 0.3.0 · `saa-pipecat-client` 0.3.0

- `PredictionEvent.responding` surfaces the server's native AI-responding flag (falls back to `source == "ai_responding"`).
- `on_warmup` fires on the server's native `warmup_complete` message.
- Standardized on the `SAA_API_KEY` environment variable across docstrings and quickstarts.
- First public release of `saa-pipecat-client` (Pipecat-on-Daily hosted bridge).

### Examples

- Added `python/` and `web/` streaming-SDK demos (subtree-merged from the standalone demo repos): the SDK driving its own capture loop end to end, with detected turns routed to OpenAI Realtime.
- The `livekit/web` and `pipecat/web` browser samples now render the native warmup and AI-responding states (the prediction card shows a distinct "responding" colour during AI playback instead of "silent").

## Streaming SDKs, 0.3.x

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

## LiveKit hosted bridge, `saa-livekit-client` 0.1.0

- Summons a hidden participant into the customer's LiveKit room that runs the classifier server-side and publishes events on the `"saa"` data topic.
- `AttentionEngine` exposes `on_prediction` / `on_vad` / `on_turn_ready` / `on_interrupt` / `on_interjection` callbacks and `mute` / `unmute` / `responding_start` / `responding_stop` / `set_threshold` actions.
- `start_attention_session`, `attention_agent_token`, and `build_attention_entrypoint` helpers.
- No ML dependencies; pure Python.

## Examples

- `examples/livekit/`, two runnable LiveKit Agents 1.5.x samples: `voice_agent_realtime` and `web`.
