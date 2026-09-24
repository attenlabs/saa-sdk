# @attenlabs/saa-js

JavaScript SDK for [Attention Labs](https://attentionlabs.ai) Selective Auditory Attention (SAA): the addressee layer for voice agents. One decision per utterance about whether speech was meant for your agent, before STT, LLM, or TTS. No wake word, model-agnostic, drop-in for the voice stack you already use.

The mental model is simple: audio in -> addressee gate (the SAA decision) -> only addressed audio out. The attention model runs on Attention Labs' service, so this is a thin Apache-2.0 client: it captures and streams your mic + camera and emits typed events. All inference runs server-side.

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
| `autoReconnect`    | boolean  | `true`                               | Reconnect with backoff after an unclean mid-session drop. Set `false` to surface the drop as an `error` instead. |
| `serverProfile`    | string   | inferred                             | Server processor variant. Defaults to `"audio_only"` when `enableVideo: false`, else the full processor. Pass `"default"` to force the full processor without local video. |
| `utteranceHandling` | boolean | `false`                              | Opt into utterance handling: per-utterance transcripts with an addressee verdict, delivered as `utteranceEnded`. See [Utterance handling](#utterance-handling). |
| `workletUrl`       | string   | bundled                              | URL of the audio-capture AudioWorklet module. Override only when self-hosting the worklet. |
| `video.width`      | number   | `1920`                               | Capture width. |
| `video.height`     | number   | `1080`                               | Capture height. |
| `video.jpegQuality`| number   | `0.5`                                | JPEG quality (0-1). |
| `audio.targetSampleRate` | number | `16000`                         | Sample rate audio is resampled to before sending. |
| `audio.onAudioFrame`| function | none                                | Called with each captured 16-bit PCM frame (`ArrayBuffer`). |
| `audio.onWorkletError`| function | none                              | Called when the capture worklet throws (also emitted as an `error` event). |
| `audio.onContextStateChange`| function | none                        | Called with the `AudioContext` state string on change (e.g. `suspended`, `interrupted`). |

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
| `addAssistantTurn(text)`    | Utterance handling: feed back what the assistant said, as spoken. Returns `false` when the socket is not open. |
| `setUtteranceThreshold(value)` | Utterance handling: the one-sided class-1 decision threshold (0-1]. Server acks via `utteranceConfig`. |
| `clearUtteranceHistory()`   | Utterance handling: forget the dialogue history (new conversation). |
| `isConnected`               | Getter — `true` while the WebSocket is open. |
| `currentThreshold`          | Getter — the current confidence threshold (0-1). |
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
| `turnReady`      | `{ audioBase64, audioPcm16, durationSec, frames, context }` |
| `config`         | `{ modelClass2Threshold }` |
| `stats`          | `{ rttMs, bufferedAmount, sentVideo, skippedVideo, sentAudio, uptimeMs }` |
| `interrupt`      | `{ fadeMs, confidence }` |
| `interjection`   | `{ reason, audioBase64, audioPcm16, durationSec }` |
| `utteranceEnded` | `{ seq, text, prediction, confidence, decision, reason, startS, endS, truncated, assistantTurns, preview, latencyMs, audioBase64, audioPcm16 }` |
| `utteranceConfig` | `{ enabled, class1Threshold, preview, reason }` |
| `error`          | `{ title, message, detail }` |
| `disconnected`   | `{ code, reason, wasClean }` |
| `reconnecting`   | `{ attempt, delaySec, lastCode }` |
| `reconnected`    | `{ attempts }` |

`warmupComplete` fires once the server model has warmed up and is producing real predictions; use it to drop any loading UI. `prediction.responding` is `true` while your app is mid-response (see `markResponding`), and `interjection` fires when the agent should volunteer after humans go quiet.

If the camera is unavailable when video capture is enabled and audio is enabled, `start()` continues with an audio-only session (`audio_only` server profile), emits an `error` with `kind: "environment"` and `title: "Camera unavailable"`. The original `enableVideo` request is restored on the next `start()`, so a later session retries video.

## Utterance handling

Opt in with `utteranceHandling: true`. The server then segments speech into utterances on voice
activity, transcribes each one when it ends, and scores whether it was aimed at the device
(`prediction: 2`) or at a person (`prediction: 1`). One `utteranceEnded` event per utterance
carries the text, the verdict and the utterance audio; `utteranceConfig` arrives after
`started` and says whether the feature is on for this session.

The classifier is conditioned on the preceding dialogue and is unreliable without it, so feed
back every assistant response, as spoken, with `addAssistantTurn(text)`. `assistantTurns` on the
event shows how many assistant lines were in the scored history; `0` means you have not fed any.

```js
const client = new AttentionClient({ token, utteranceHandling: true });

client.on("utteranceEnded", (u) => {
  console.log(u.text, u.prediction, u.confidence, u.decision, u.preview);
});

// after each assistant response has been spoken
client.addAssistantTurn(assistantTranscript);
```

This stream is independent of `turnReady`: the two will not line up one to one (a turn can hold
two utterances, an utterance can straddle a class change). Drive your LLM from one or the
other. While the feature is in preview, `preview` is `true` and the verdict is preview grade; the server delivers transcripts and does not retain them.

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
