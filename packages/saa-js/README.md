# @attenlabs/saa-js

JavaScript SDK for [attention labs](https://attentionlabs.ai) Selective Auditory Attention (SAA): the addressee layer for voice agents. One decision per utterance about whether speech was meant for your agent, before STT, LLM, or TTS. No wake word, model-agnostic, drop-in for the voice stack you already use.

The mental model is simple: audio in -> addressee gate (the SAA decision) -> only addressed audio out. The attention model runs on attention labs' service, so this is a thin Apache-2.0 client: it captures and streams your mic + camera and emits typed events. All inference runs server-side.

## Sign up

Get your API key at [attentionlabs.ai](https://attentionlabs.ai).

## Install

```bash
npm install @attenlabs/saa-js
```

## Quick start

```ts
import { AttentionClient } from "@attenlabs/saa-js";

const videoEl = document.querySelector("video");

const client = new AttentionClient({
  token: "your-api-key",
});

client.on("prediction", ({ cls, confidence, source, numFaces }) => {
  console.log(`${cls}: ${confidence.toFixed(2)}`);
});

client.on("turnReady", ({ audioBase64, durationSec }) => {
  // Forward the captured turn to your LLM of choice
});

await client.start({ videoElement: videoEl });
```

## Options

| Option             | Type     | Default                              | Description |
| ------------------ | -------- | ------------------------------------ | ----------- |
| `token`            | string   | none                                    | Your API key from attentionlabs.ai. |
| `initialThreshold` | number   | `0.7`                                | Confidence threshold for predictions (0-1). |
| `enableAudio`      | boolean  | `true`                               | Capture the mic internally. Set `false` to push audio via `feedAudio()`. |
| `enableVideo`      | boolean  | `true`                               | Capture the camera internally. Set `false` for audio-only or to push frames via `feedVideo()`. |
| `serverProfile`    | string   | inferred                             | Server processor variant. Defaults to `"audio_only"` when `enableVideo: false`, else the full processor. Pass `"default"` to force the full processor without local video. |
| `video.width`      | number   | `1920`                               | Capture width. |
| `video.height`     | number   | `1080`                               | Capture height. |
| `video.jpegQuality`| number   | `0.5`                                | JPEG quality (0-1). |

## Methods

| Method                      | Description |
| --------------------------- | ----------- |
| `start({ videoElement, mediaStream? })` | Start streaming + connect. Calls `getUserMedia` unless `mediaStream` is supplied. `videoElement` is required when video capture is enabled. |
| `stop()`                    | Stop streaming and disconnect. |
| `feedAudio(audio, sampleRate?)` | Push externally-captured audio (requires `enableAudio: false`). Accepts Float32 `[-1,1]`, Int16 PCM, or a raw int16 buffer; re-chunked + resampled to the wire's 16 kHz / 100 ms blocks. See [External capture](#external-capture). |
| `feedVideo(jpeg)`           | Push an externally-captured JPEG frame (requires `enableVideo: false`). Accepts a `Blob`, `ArrayBuffer`, or view. |
| `mute()` / `unmute()`       | Pause or resume audio. |
| `markResponding(boolean)`   | Signal that your app is responding, pauses predictions until finished. |
| `setThreshold(value)`       | Update the confidence threshold (0-1). |
| `on(event, listener)`       | Subscribe to an event. Returns an unsubscribe function. |

## Events

| Event            | Payload |
| ---------------- | ------- |
| `connected`      | none |
| `started`        | none |
| `warmupComplete` | none |
| `prediction`     | `{ cls, rawCls, confidence, source, numFaces, responding }` |
| `vad`            | `{ probability, isSpeech }` |
| `state`          | `{ state }` (one of `listening`, `sending`, `cancelled`, `idle`) |
| `turnReady`      | `{ audioBase64, audioPcm16, durationSec, serverTurnReadyTsMs, frames, context }` — see [field types](#turnready-fields) below |
| `config`         | `{ modelClass2Threshold }` |
| `stats`          | `{ rttMs, bufferedAmount, sentVideo, skippedVideo, sentAudio, uptimeMs }` |
| `interrupt`      | `{ fadeMs, confidence }` |
| `interjection`   | `{ reason, audioBase64, audioPcm16, durationSec }` |
| `error`          | `{ title, message, detail }` |
| `disconnected`   | `{ code, reason }` |

`warmupComplete` fires once the server model has warmed up and is producing real predictions; use it to drop any loading UI. `prediction.responding` is `true` while your app is mid-response (see `markResponding`), and `interjection` fires when the agent should volunteer after humans go quiet.

### `turnReady` fields

| Field | Type | Description |
| --- | --- | --- |
| `audioBase64` | `string` | Turn audio, PCM16 @ 16 kHz mono, base64-encoded. |
| `audioPcm16` | `Int16Array` | Same audio, decoded to a typed array. |
| `durationSec` | `number` | Turn duration in seconds. |
| `serverTurnReadyTsMs` | `number \| null` | Server-clock timestamp (ms) when the turn was emitted; useful for latency measurement. `null` on older servers that don't send it. |
| `frames` | `Array<{ tsOffsetS: number; imageBase64: string }>` | Video frames captured during the turn. Empty array in audio-only mode. `tsOffsetS` is seconds from turn start. |
| `context` | `string \| null` | Set by the server for special cases, e.g. `"interjection_follow_up"`. `null` for a normal turn. |

```ts
client.on("turnReady", (turn) => {
  yourSTT.send(turn.audioBase64);
  console.log(`turn was ${turn.durationSec}s, ${turn.frames.length} video frames`);
});
```

## LLM integration

The SDK captures speech but does **not** route it to an LLM. SAA is model-agnostic and drop-in: use the `turnReady` event to forward only device-directed audio to any model, ASR, or voice stack you like.

When your LLM starts responding, call `client.mute()` and `client.markResponding(true)`. When it finishes, call `client.unmute()` and `client.markResponding(false)`.

## External capture

By default the SDK opens its own mic + camera. To run on capture you already
own there are two paths:

**Share a `MediaStream`** (the SDK reads it but won't stop its tracks):

```ts
const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
videoEl.srcObject = stream;                 // your app renders it
await client.start({ videoElement: videoEl, mediaStream: stream });
// ... another consumer (e.g. a gaze SDK) reads the same stream / videoEl
```

**Push frames yourself** (no `getUserMedia` at all) for taps, Twilio media,
or non-browser sources:

```ts
const client = new AttentionClient({ token, enableAudio: false, enableVideo: false });
await client.start();                        // opens the WS, captures nothing
client.feedAudio(pcmChunk);                  // Float32 [-1,1] | Int16 | int16 buffer
client.feedAudio(pcm48k, 48000);             // resampled to 16 kHz
client.feedVideo(jpegBlob);                  // Blob | ArrayBuffer | view
```

Mix and match: `enableVideo: false` with internal mic for audio-only, or
`enableAudio: false` + `feedAudio()` while the SDK still grabs camera frames.

## License

Apache-2.0
