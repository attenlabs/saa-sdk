# @attenlabs/saa-js

JavaScript SDK for [Attention Labs](https://attentionlabs.ai) real-time attention detection.

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
  token: "your-auth-token",
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
| `token`            | string   | —                                    | Your API key from attentionlabs.ai. |
| `initialThreshold` | number   | `0.7`                                | Confidence threshold for predictions (0–1). |
| `video.width`      | number   | `1920`                               | Capture width. |
| `video.height`     | number   | `1080`                               | Capture height. |
| `video.jpegQuality`| number   | `0.6`                                | JPEG quality (0–1). |

## Methods

| Method                      | Description |
| --------------------------- | ----------- |
| `start({ videoElement })`   | Start streaming. Requests mic + camera access and connects to the server. |
| `stop()`                    | Stop streaming and disconnect. |
| `mute()` / `unmute()`       | Pause or resume audio. |
| `markResponding(boolean)`   | Signal that your app is responding — pauses predictions until finished. |
| `setThreshold(value)`       | Update the confidence threshold (0–1). |
| `on(event, listener)`       | Subscribe to an event. Returns an unsubscribe function. |

## Events

| Event            | Payload |
| ---------------- | ------- |
| `connected`      | — |
| `started`        | — |
| `prediction`     | `{ cls, confidence, source, numFaces }` |
| `vad`            | `{ probability, isSpeech }` |
| `state`          | `{ state }` — one of `listening`, `sending`, `cancelled`, `idle` |
| `turnReady`      | `{ audioBase64, audioPcm16, durationSec, frames, context }` |
| `interrupt`      | `{ fadeMs, confidence }`|
| `error`          | `{ title, message, detail }` |
| `disconnected`   | `{ code, reason }` |

## LLM integration

The SDK captures speech but does **not** route it to an LLM. Use the `turnReady` event to forward audio to any model you like.

When your LLM starts responding, call `client.mute()` and `client.markResponding(true)`. When it finishes, call `client.unmute()` and `client.markResponding(false)`.

## License

Apache-2.0
