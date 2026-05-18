<p align="center">
  <a href="../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# pipecat

A drop-in `SAAGate` `FrameProcessor` that sits **before STT** in any Pipecat 1.x pipeline. Inbound `AudioRawFrame`s pass through SAA; only device-directed segments propagate downstream.

| | |
|---|---|
| Run             | `make dev` (laptop mic + camera) or `make cloud TRANSPORT=daily\|smallwebrtc\|twilio` |
| Demonstrates    | `SAAGate(FrameProcessor)` placed before STT in a Pipecat pipeline. Sidecar `SAA*Frame`s for observability. `InterruptionFrame` for barge-in. |
| Expect          | Standard Pipecat pipeline + an extra processor. No fork, no patch, no SDK surgery. |
| Known gotcha    | The gate's audio buffering interacts with Pipecat's frame cadence; defaults are tuned for 16 kHz mono in. Re-tune if you change the transport sample rate. |

## Run

```bash
cp .env.example .env       # ATTENLABS_TOKEN + Deepgram + OpenAI + Cartesia keys
make install
make dev                   # local-audio bot, laptop mic + camera
```

For cloud transports: `make cloud TRANSPORT=daily` (or `smallwebrtc`, `twilio`).

For a live observability overlay alongside the bot: `make overlay`.

## Two modes (one gate)

The gate runs in **upstream mode**: it sits before STT, makes the routing decision, and either forwards the frame or drops it before any downstream cost. The wiring is identical in either configuration below:

- **Pass-through gate**: drops every audio frame that arrives outside an addressed turn. Lowest STT cost.
- **Sidecar observer**: emits four typed frames (`SAAPredictionFrame`, `SAADecisionFrame`, `SAAStatsFrame`, `SAAConnectionFrame`) for every prediction / VAD / state / connection event so a downstream `FrameObserver` can record or dashboard. Combine with the live overlay (`make overlay`) for a flight-recorder view.

Flip via `SAA_EMIT_SIDECAR` (default `true`).

## Env vars

| Var | Required | Default |
|---|---|---|
| `ATTENLABS_TOKEN` | yes | |
| `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, `CARTESIA_API_KEY` | yes | |
| `SAA_THRESHOLD` | no | `0.7` |
| `SAA_FORWARD_VIDEO` | no | `true` |
| `SAA_BARGE_IN` | no | `true` |
| `SAA_EMIT_SIDECAR` | no | `true` |
| `SAA_PASSTHROUGH_WARMUP` | no | `false` |
| `OPENAI_MODEL` | no | `gpt-4o-mini` |
| `CARTESIA_VOICE_ID` | no | (default voice) |
| `SAA_OVERLAY_PORT` | no | `8080` |

## Files

- [`saa_gate.py`](./saa_gate.py): the `SAAGate` `FrameProcessor` and its `SAA*Frame` sidecars. Calls `feed_audio` on every 16 kHz frame and `feed_video` when `SAA_FORWARD_VIDEO=true`.
- [`bot.py`](./bot.py): reference Pipecat agent shaped as `transport.input()` → VAD → SAA → STT → LLM → TTS. Launched via `pipecat-runner` (`make dev` / `make cloud`) or directly with `python bot.py`.
- [`requirements.txt`](./requirements.txt): pins `pipecat-ai` + `attenlabs-saa`.
- [`overlay_server.py`](./overlay_server.py): optional SSE shim that fans the gate's decision frames out to `@attenlabs/saa-overlay`.

## Tests

```bash
make test-shape          # no-network shape check (CI uses this)
make test                # full pytest, needs pipecat-ai installed
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Pipecat receives no frames after SAA | Check `ATTENLABS_TOKEN` is set; the gate fails closed when the cloud session can't open. |
| Barge-in doesn't fire | Verify `SAA_BARGE_IN=true` and that your transport surfaces `InterruptionFrame` upstream. |
| Overlay shows nothing | The overlay subscribes to sidecar frames; `SAA_EMIT_SIDECAR` must be `true`. |

## See also

- [`packages/saa-py/README.md`](../../packages/saa-py/README.md): SDK reference.
- [`packages/saa-gate/README.md`](../../packages/saa-gate/README.md): the cross-framework routing policy.
- [`examples/proactive-agent/pipecat/`](../proactive-agent/pipecat/): the proactive overlay built on this adapter.
- [`examples/README.md`](../README.md): adapter index.


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
