# Claude orientation

If you are Claude (or another AI coding assistant) reading this repo, start here. The multi-vendor twin is [`AGENTS.md`](./AGENTS.md), which carries the same contract plus the seven-line integration recipes.

## What this repo is

The SAA monorepo. SAA stands for **Selective Auditory Attention** — a pre-ASR addressee gate that decides whether speech in a room was meant for the device, before STT fires. The repo ships two cloud SDKs and a small fleet of framework adapters that wire SAA into the voice-agent stacks customers already use.

The technical report describing SAA's architecture and evaluation is [arXiv:2604.08412](https://arxiv.org/abs/2604.08412). The paper uses the original acronym “SAS”; the system has since been rebranded to “SAA”.

## What's in this repo

```
packages/
  saa-js                  — @attenlabs/saa-js cloud SDK (browser)
  saa-py                  — attenlabs-saa cloud SDK (Python)
  saa-gate                — routing-policy state machine
  saa-proactive-js,
  saa-proactive-py        — outbound-voice lifecycle wrappers

examples/                 — drop-in adapters per framework:
  twilio, pipecat, livekit, openai-realtime, elevenlabs-cai,
  proactive-agent (5 stack overlays),
  cloud-live-demo, production-gate, obs-overlay
```

Every adapter in `examples/` consumes the public cloud SDK (`@attenlabs/saa-js` or `attenlabs-saa`). No private model artifact ships in this repo.

## Shapes that are intentionally not part of this repo

Do not introduce any of the following — they are either degraded mimics of the cloud classifier or depend on cloud-side surfaces that do not exist:

- A local deterministic addressee scorer ("saa-tiny" or similar). The cloud SDK with `enable_video=False` is the audio-only path.
- A `<saa-mic>` Web Component or React-hook wrapper that promises addressee detection without a token.
- An MCP server wrapping a local classifier. The cloud is streaming-only; MCP is request-response.
- A client-side token-mint / control-plane SDK. Tokens are issued via the dashboard.
- A local AP/1 daemon or any public protocol spec. The cloud wire is custom binary tags (`MSG_AUDIO=0x01`, `MSG_VIDEO=0x02`) plus JSON control; no public spec.
- Trace fan-out, trace-schema packages, or dev recording / replay tooling. The SDK's `stats` event is the only observability surface.
- A model card claiming on-device deployment from the open SDK. On-device deployment is a separate enterprise OEM licence.

If a customer genuinely needs MCP or a control-plane SDK, that's a server-side request (`POST /score`, dashboard public API), not a client wrapper.

## The product seam to honour

The cloud SDK emits a small typed event surface: `prediction`, `vad`, `state`, `speechReady`, `config`, `stats`, `error`. Adapters consume those events and translate them into framework-native shapes (Pipecat `FrameProcessor`, LiveKit plugin, Twilio bridge, etc.). That's the entire product seam.

- Don't invent new event shapes in `packages/saa-js` or `packages/saa-py`.
- Don't ship classifier code, model weights, or training data. The classifier is a commercial asset.
- Don't introduce “AP/1” naming or wire formats.

## Where numbers live

Accuracy / latency / footprint / cost-reduction figures all live in the technical report ([arXiv:2604.08412](https://arxiv.org/abs/2604.08412)). Cite the paper rather than restating figures in README copy.

Keep the paper's framing: *“not state-of-the-art across all DDSD settings”*, *“not directly comparable to TTM mAP”*. Match that posture in user-facing content.

## What you must not claim

- Don't promise data, weights, ONNX, or training corpora.
- Don't write “reproduce our benchmark” scripts that ship recordings or labels.
- Don't claim on-device deployment from the open SDK alone. The open SDKs stream to the cloud. On-device is the OEM SDK, separately licensed.
- Don't claim a public cross-vendor leaderboard. None exists.

## When the user asks about adapters

The canonical place to look is [`examples/`](./examples/). Each subdir has its own README, Dockerfile, Makefile, and smoke tests. The adapter index is [`examples/README.md`](./examples/README.md). If you need to build a new adapter, copy the closest existing one (Twilio for telephony, Pipecat for frame-pipeline frameworks, LiveKit for plugin-style frameworks) and adapt.

## Tone

When you write user-facing content in this repo, keep the paper's posture: precise, humble about scope, specific about operating points. Do not add hype on top of the headline numbers.
