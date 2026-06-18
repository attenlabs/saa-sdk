# AGENTS.md

A short contract for **AI coding agents** (Cursor, Aider, Continue, Cline, Codex, Claude Code, GitHub Copilot Workspace, Devin, etc.) working *inside this repo* or *integrating SAA into another repo*.

The human-facing README is [`README.md`](./README.md); the assistant-facing twin is [`CLAUDE.md`](./CLAUDE.md). This file is the lowest-common-denominator contract — the same facts in a shape any agent can consume.

## What SAA is

**Selective Auditory Attention.** A pre-ASR addressee gate. SAA decides whether speech in the room was meant for the device, before STT fires. One classification per VAD-positive utterance, fail-closed.

## Three integration shapes

1. **Streaming SDK** — `@attenlabs/saa-js` (npm; browser, Node, Deno, Bun) and `attenlabs-saa` (PyPI; CPython 3.10+). Your app captures audio/video, streams it to the SAA cloud over WebSocket, and consumes typed events.
2. **LiveKit hosted bridge** — `saa-livekit-client` (PyPI). A hidden participant joins the customer's LiveKit room, runs the classifier server-side, and publishes events on the `"saa"` data topic. Examples live in [`examples/livekit/`](./examples/livekit/).
3. **Pipecat hosted bridge** — `saa-pipecat-client` (PyPI). Same shape on Daily for Pipecat voice agents: a hidden participant joins the customer's Daily room and publishes events on the `"saa"` Daily app-message topic. The `AttentionEngine` subscribes via the consumer's `DailyTransport`. Examples in [`examples/pipecat/`](./examples/pipecat/).

## Where numbers live

Accuracy / latency / footprint / cost-reduction figures all live in the technical report ([arXiv:2604.08412](https://arxiv.org/abs/2604.08412)). Cite the paper rather than restating figures in marketing-shaped tables.

Keep the paper's framing: *“not state-of-the-art across all DDSD settings”*, *“not directly comparable to TTM mAP”*. Match that posture in user-facing copy.

## Hard rules

### Do not invent

- **No local classifier.** There is no on-device scorer in the open SDKs. The cloud SDK with `enable_video=False` is the audio-only path. On-device deployment is a separate enterprise OEM licence.
- **No client-side token-mint / control-plane SDK.** API keys come from Attention Labs, validated server-side.
- **No public wire-protocol spec.** The cloud wire is custom binary tags plus JSON control; there is no published protocol document.
- **`feed_audio` exists in `attenlabs-saa` (>= 0.4.0); `feed_video` and any `@attenlabs/saa-js` feed API do not.** Construct the Python client with `enable_audio=False` and push PCM via `feed_audio` to gate audio a framework already captured (the ElevenLabs sample does this). The streaming SDKs still self-capture by default; JS has no feed path yet, and there's no external *video* ingestion anywhere — don't assume those are shipped.
- **No `speechReady` / `on_speech_ready` event.** The "utterance ready" event is **`turnReady`** (JS) / **`turn_ready`** (Python) → a `TurnReadyEvent`.
- **No `saa-gate`, `saa-proactive-js`, or `saa-proactive-py` packages.** They do not exist. Proactive lifecycle is `markResponding` / `mark_responding` on the existing SDKs.


## Where numbers live

Accuracy / latency / footprint / cost figures are **not published in this repo**. Do not invent benchmark tables or cite a paper that isn't linked here. If a number is needed, leave it to Attention Labs to publish.

## Repository layout

```
packages/
  saa-js                 — @attenlabs/saa-js streaming SDK (browser + Node)
  saa-py                 — attenlabs-saa streaming SDK (Python)
  saa-livekit-client     — LiveKit hosted-bridge client (Python)
  saa-pipecat-client     — Pipecat (on Daily) hosted-bridge client (Python)
examples/
  livekit/               — 3 runnable LiveKit samples (cascaded, realtime, web)
  pipecat/               — 2 runnable Pipecat-on-Daily samples (cascaded, web)
```

Every file in `examples/` consumes a public package. No example depends on a private artifact.

## Recipes

### Streaming SDK — JavaScript / browser

```js
import { AttentionClient } from "@attenlabs/saa-js";

const client = new AttentionClient({ token: process.env.SAA_API_KEY });
client.on("turnReady", (turn) => downstream(turn.audioBase64));
await client.start({ videoElement: document.querySelector("video") });
```

### Streaming SDK — Python

```python
import os
from saa import AttentionClient

client = AttentionClient(token=os.environ["SAA_API_KEY"])

@client.on_turn_ready
def _(turn):
    downstream(turn.audio_base64)   # or turn.audio_pcm16 (np.int16)

client.start()
```

### Audio-only (telephony, pendants, single-mic kiosks)

```python
client = AttentionClient(token=os.environ["SAA_API_KEY"], enable_video=False)
# same event surface; the cloud falls back to the audio-only operating point
```

### LiveKit Agents (hosted bridge)

See [`examples/livekit/`](./examples/livekit/). Summon the agent with `start_attention_session(...)`, then gate the session in `@engine.on_prediction` via `session.input.set_audio_enabled(p.aligned_class == 2)`. Barge-in is `session.interrupt()` on `@engine.on_interrupt`.

### Pipecat on Daily (hosted bridge)

See [`examples/pipecat/`](./examples/pipecat/). Same call shape as the LiveKit bridge — `start_attention_session(...)` summons the hidden agent into the consumer's Daily room — but pass `room_url=...` instead of `livekit_url`+`room_name`. Construct the engine with `AttentionEngine(transport, agent_identity=session.agent_identity)` where `transport` is the consumer's `DailyTransport` (the engine hooks `on_app_message` on it). Upstream actions (`responding_start`, `set_threshold`, …) queue a `DailyOutputTransportMessageUrgentFrame` onto a bound `PipelineTask`, so call `engine.bind_task(task)` once the task is built. Barge-in in Pipecat 1.x is `await task.queue_frames([InterruptionTaskFrame()])` on `@engine.on_interrupt`.

### Proactive (outbound voice)

Streaming SDK: `markResponding(true)` / `mark_responding(True)` before the agent speaks, `false` after. LiveKit and Pipecat bridges: `engine.responding_start()` / `responding_stop()` — identical surface.

## When you write code in this repo

- Apache-2.0 SPDX header on every new source file.
- New `packages/<x>` entries are for client libraries that wrap the cloud SDK for a framework the customer can't trivially write themselves. Otherwise add an `examples/<topic>/` entry.

## When you write content in this repo

- Don't promise data, weights, ONNX, or training corpora.
- Adapter availability is whatever's in [`examples/`](./examples/). Don't list adapters that don't exist as code — the roadmap stacks are roadmap, not shipped.
- The OEM on-device path is *separate licensing* via [attentionlabs.ai](https://attentionlabs.ai). Don't conflate it with the open SDKs.

## Where to look

- Streaming SDK reference → [`packages/saa-js/README.md`](./packages/saa-js/README.md), [`packages/saa-py/README.md`](./packages/saa-py/README.md)
- LiveKit hosted bridge → [`packages/saa-livekit-client/README.md`](./packages/saa-livekit-client/README.md), [`examples/livekit/`](./examples/livekit/)
- Pipecat-on-Daily hosted bridge → [`packages/saa-pipecat-client/README.md`](./packages/saa-pipecat-client/README.md), [`examples/pipecat/`](./examples/pipecat/)
- Example index → [`examples/README.md`](./examples/README.md)
