<p align="center">
  <a href="../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# livekit

SAA is a pre-STT gate for a LiveKit Agents worker. The gate overrides `Agent.stt_node` so STT only fires on device-directed turns. When a participant has a video track, frames are forwarded to `AttentionClient.feed_video` for multimodal accuracy.

| | |
|---|---|
| Run             | `make dev` (or `make console` for a no-room dry run) |
| Demonstrates    | A LiveKit Agents worker with a pre-STT SAA gate, function-tool driven threshold + mute control, and per-turn SAA metadata in the chat context. |
| Expect          | Standard LiveKit Agents lifecycle: room subscribe, participant join, agent speaks. The gate sits inside `stt_node` and is invisible to downstream callers. |
| Known gotcha    | Two modes, **`pre_stt`** (recommended) and **`response`** (suppress responses after STT runs). Pick `pre_stt` unless the LiveKit version pins STT for you. |

## Run

```bash
cp .env.example .env
make install
make download-files        # Silero VAD etc.
make dev                   # worker watches for file changes
```

For a one-off connection: `make connect ROOM=demo-1`.
For a headless smoke run with no LiveKit room: `make console`.

The optional `serve_token.py` JWT mint helper has its own dependencies (`fastapi`, `uvicorn`, `livekit-api`) that aren't in `requirements.txt`; install them via the pyproject's `[serve]` extra:

```bash
make install-serve         # pip install -e ".[serve]"
make serve-token           # JWT mint helper at http://localhost:8088
```

## Two modes

| Mode | When | Wiring |
|---|---|---|
| **`pre_stt`** (default) | LiveKit Agents ≥ 1.0 with overridable `stt_node` | Gate sits between participant audio track and STT. STT only sees device-directed turns. Lowest cost. |
| **`response`** | LiveKit pins STT before agent code can intercept | Gate suppresses agent **response** after the LLM is already invoked. Wasted STT, but the assistant still doesn't speak over side conversations. |

Flip via `SAA_GATE_MODE`.

## Env vars

| Var | Required | Default |
|---|---|---|
| `ATTENLABS_TOKEN` | yes | |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | yes | |
| `OPENAI_API_KEY` | yes (LLM) | |
| `DEEPGRAM_API_KEY` | yes (STT) | |
| `SAA_GATE_MODE` | no | `pre_stt` |
| `SAA_THRESHOLD` | no | `0.7` |
| `SAA_GATE_TTL_S` | no | `2.0` |
| `SAA_ENABLE_VIDEO` | no | `true` |
| `SAA_VIDEO_FPS`, `SAA_VIDEO_JPEG_QUALITY` | no | `4`, `60` |
| `OPENAI_MODEL`, `OPENAI_VOICE` | no | `gpt-4o-mini`, `alloy` |
| `DEEPGRAM_MODEL` | no | `nova-2-general` |
| `AGENT_NAME` | no | `saa-gated-assistant` |
| `TOKEN_HOST`, `TOKEN_PORT`, `TOKEN_TTL_S` | no | `0.0.0.0`, `8088`, `3600` |

## Files

- [`agent.py`](./agent.py): the LiveKit Agents worker with the SAA gate wired into `stt_node`.
- [`saa_gate.py`](./saa_gate.py): the gate itself, covering `AttentionClient` lifecycle, per-participant routing, and per-turn metadata.
- [`serve_token.py`](./serve_token.py): JWT mint helper for browser clients (kept off the worker by default).

## Tests

```bash
make test-shape          # no-network shape check (CI uses this)
make test                # full pytest, needs livekit-agents installed
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Worker connects but the agent never speaks | Check `ATTENLABS_TOKEN`. The gate fails closed when SAA can't open. |
| Browser can't get a token | `make serve-token` and point your client at `http://localhost:8088`. |
| Video isn't gating | `SAA_ENABLE_VIDEO=true` and the participant must publish a video track. |

## See also

- [`packages/saa-py/README.md`](../../packages/saa-py/README.md): SDK reference.
- [`packages/saa-gate/README.md`](../../packages/saa-gate/README.md): the cross-framework routing policy.
- [`examples/proactive-agent/livekit/`](../proactive-agent/livekit/): the proactive overlay built on this adapter.
- [`examples/README.md`](../README.md): adapter index.


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
