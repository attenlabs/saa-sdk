# AGENTS.md

A short contract for **AI coding agents** (Cursor, Aider, Continue, Cline, Codex, Claude Code, GitHub Copilot Workspace, Devin, etc.) working *inside this repo* or *integrating SAA into another repo*.

The human-facing README is [`README.md`](./README.md); the assistant-facing twin is [`CLAUDE.md`](./CLAUDE.md). This file is the lowest-common-denominator contract, the same facts in a shape any agent can consume.

## What SAA is

**Selective Auditory Attention.** Pre-ASR addressee gate. SAA decides whether speech in the room was meant for the device, before STT fires. One classification per VAD-positive utterance, fail-closed.

Two cloud SDKs, one per language; both wrap the same hosted classifier:

- `@attenlabs/saa-js` on npm — browser, Node, Deno, Bun.
- `attenlabs-saa` on PyPI — CPython 3.10+.

Five framework adapters in [`examples/`](./examples/): Twilio Media Streams, Pipecat, LiveKit Agents, OpenAI Realtime, ElevenLabs Conversational AI — plus a proactive-agent overlay for each.

## Where numbers live

Accuracy / latency / footprint / cost-reduction figures all live in the technical report ([arXiv:2604.08412](https://arxiv.org/abs/2604.08412)). Cite the paper rather than restating figures in marketing-shaped tables.

Keep the paper's framing: *“not state-of-the-art across all DDSD settings”*, *“not directly comparable to TTM mAP”*. Match that posture in user-facing copy.

## Hard rules

### Do not invent

- **No local deterministic scorer** (saa-tiny-style). The cloud SDK with `enable_video=False` is the audio-only path.
- **No `<saa-mic>` Web Component or React hook** that promises addressee detection without a token.
- **No AP/1 spec.** The cloud wire is custom binary tags (`MSG_AUDIO=0x01`, `MSG_VIDEO=0x02`) plus JSON control. There is no public protocol spec.
- **No client-side token-mint / control-plane SDK.** Tokens come from the dashboard, validated server-side.
- **No MCP server wrapping a local classifier.** The cloud is streaming-only; MCP is request-response. Revisit only if a `POST /score` cloud endpoint ships.
- **No traces, trace-schema, or observability fan-out** beyond what the SDK already exposes (`stats` events).
- **No model card claiming on-device deployment from the open SDK.** Open SDKs stream to the cloud. On-device is the OEM SDK, separately licensed.

### Do not leak

- **No audio, video, or recording artifacts** anywhere in this repo. Extensions to watch: `*.wav`, `*.mp3`, `*.flac`, `*.ogg`, `*.opus`, `*.pcm`, `*.pcm16`.
- **No model weights** anywhere: `*.onnx`, `*.pt`, `*.pth`, `*.tflite`, `*.safetensors`, `*.ckpt`, `*.bin`, `*.task`.
- **No labelled corpora**, no “reproduce our benchmark” scripts.
- **No internal dataset composition details** beyond what the public paper already states.

## Repository layout

```
packages/
  saa-js, saa-py                     — cloud SDKs (browser + Python)
  saa-gate                           — routing-policy state machine
  saa-proactive-js, saa-proactive-py — outbound-voice lifecycle
examples/                            — framework adapters
```

Every file in `examples/` consumes the public SDK. No example depends on a private artifact.

## Recipes (the seven-line core)

### JavaScript / Browser

```js
import { AttentionClient } from "@attenlabs/saa-js";

const client = new AttentionClient({ token: process.env.ATTENLABS_TOKEN });
client.on("speechReady", (e) => downstream(e.audioBase64));
await client.start({ videoElement: document.querySelector("video") });
```

### Python

```python
import os
from saa import AttentionClient

client = AttentionClient(token=os.environ["ATTENLABS_TOKEN"])

@client.on_speech_ready
def _(e):
    downstream(e.audio_pcm16)

client.start()
```

### Audio-only (telephony, AI pendants, single-mic kiosks)

```python
client = AttentionClient(token=os.environ["ATTENLABS_TOKEN"], enable_video=False)
# Same event surface; the cloud falls back to the audio-only operating point.
```

### Telephony (Twilio Media Streams)

See [`examples/twilio/`](./examples/twilio/). The μ-law 8 kHz → PCM16 16 kHz transcode lives in [`audio.py`](./examples/twilio/audio.py); the SAA glue is in [`server.py`](./examples/twilio/server.py).

### Pipecat

See [`examples/pipecat/saa_gate.py`](./examples/pipecat/saa_gate.py) for the `FrameProcessor`-style gate. Drop it before STT.

### LiveKit Agents

See [`examples/livekit/saa_gate.py`](./examples/livekit/saa_gate.py). Override `Agent.stt_node` with the gate; forward camera frames to `AttentionClient.feed_video` when participants have video.

### Proactive (outbound voice)

Use `mark_responding(True)` / `markResponding(true)` before the agent speaks; `False` / `false` after. The lifecycle wrapper in [`packages/saa-proactive-js`](./packages/saa-proactive-js/) and [`packages/saa-proactive-py`](./packages/saa-proactive-py/) handles try/finally and tail-ms semantics. Stack-specific overlays under [`examples/proactive-agent/`](./examples/proactive-agent/).

## When you write code in this repo

- Apache-2.0 SPDX header on every new source file.
- If you're tempted to add a new package under `packages/`, ask: does this extend the cloud SDK's value through a customer-framework adapter the customer can't write themselves in 20 minutes? If no, add an `examples/<topic>/` entry instead.

## When you write content in this repo

- Numbers come from the paper ([arXiv:2604.08412](https://arxiv.org/abs/2604.08412)). Don't re-derive.
- The benchmark reproduction path is “use your own recordings against the cloud.” Don't promise data.
- Adapter availability is whatever's in [`examples/`](./examples/). Don't list adapters that don't exist as code.
- The OEM on-device path is *separate licensing* through [attentionlabs.ai](https://attentionlabs.ai). Don't conflate it with the open SDKs.
- The paper uses “SAS”; everything else uses “SAA.” When citing the paper, footnote the rename.

## Where to look for things

- Per-stack drop-in → [`examples/README.md`](./examples/README.md) → [`examples/<stack>/`](./examples/)
- SDK reference → [`packages/saa-js/README.md`](./packages/saa-js/README.md), [`packages/saa-py/README.md`](./packages/saa-py/README.md)
- Production routing → [`packages/saa-gate/`](./packages/saa-gate/), [`examples/production-gate/`](./examples/production-gate/)
- Observability → [`examples/obs-overlay/`](./examples/obs-overlay/)
- Paper / numbers / methodology → [arXiv:2604.08412](https://arxiv.org/abs/2604.08412)
- Hosted reference docs → [attentionlabs.ai/docs](https://attentionlabs.ai/docs)
