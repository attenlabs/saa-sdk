<p align="center">
  <a href="../../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# proactive-agent / livekit

**Proactive in-room agent on LiveKit.** Thin overlay on [`examples/livekit/`](../../livekit/) that adds an HTTP `POST /trigger` sidecar so the agent can also speak first in response to back-end events (a scheduler firing, a quiet-room detector, a CRM webhook).

| | |
|---|---|
| Run             | `make dev` (starts the LiveKit worker + HTTP `/trigger` sidecar on `:8765`), then `INSTRUCTIONS="…" make trigger` |
| Demonstrates    | Mid-session proactive turn dispatched via `session.generate_reply(instructions=...)`; the parent's `agent_state_changed` handler drives `mark_responding(True/False)` on TTS transitions; per-participant audio gated independently by SAA. |
| Expect          | The agent speaks the proactive line in-room; gaze + face presence from each participant's camera continues to refine the addressee verdict; only device-directed turns reach the STT plugin. |
| Built on        | [`examples/livekit/`](../../livekit/): `SAAAudioBridge` (pre-STT gate), `agent_state_changed → mark_responding` wiring, Deepgram + OpenAI + Silero plug-ins, JPEG video forwarding. Nothing duplicated. |

## Trigger → Without SAA → With SAA

| Trigger | Without SAA | With SAA |
|---|---|---|
| `POST /trigger` from a scheduler firing, a quiet-room detector, or a CRM webhook. | The proactive TTS plays; every participant's mic then feeds the agent: cross-talk, off-camera coworkers, the agent's own playback echo. | The proactive TTS plays; per-participant SAA verdicts plus `mark_responding(True/False)` ensure only an addressee's turn reaches Deepgram. |

## What this overlay adds

- [`proactive_agent.py`](./proactive_agent.py): the parent agent plus an HTTP `/trigger` sidecar that posts to an asyncio queue, which `session.generate_reply(instructions=...)` consumes.
- [`demo_script.json`](./demo_script.json): operator-edited campaign script.

The parent's `agent_state_changed` handler drives `mark_responding(True/False)` on TTS transitions, so SAA is gated correctly across the proactive turn without any new code in this overlay.

## Run it

```bash
cd examples/proactive-agent/livekit
make install
cp .env.example .env && $EDITOR .env
make dev
```

The worker registers with LiveKit; the agent greets each participant on join. Fire a mid-session proactive turn:

```bash
make trigger
INSTRUCTIONS="Reminder: standup in 5 minutes." make trigger
curl -X POST http://localhost:8765/trigger \
  -H 'content-type: application/json' \
  -d '{"instructions": "Time check: 50 minutes elapsed."}'
```

## Common failures

| Symptom | Fix |
|---|---|
| `proactive_agent.py dev` exits with `Missing required environment variables` | One of `ATTENLABS_TOKEN`, `OPENAI_API_KEY`, `DEEPGRAM_API_KEY` is unset. |
| Worker registers but never joins a room | `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` mismatch. Generate a fresh API key from <https://cloud.livekit.io/>. |
| `make trigger` returns `200 ok` but agent is silent | No active `AgentSession`. Join a room first (or use `console` mode for local testing). |
| Camera frames never reach SAA | `SAA_ENABLE_VIDEO=false` or Pillow missing. `pip install Pillow`, set `SAA_ENABLE_VIDEO=true`. |
| Port 8765 collision | Set `PROACTIVE_HTTP_PORT=8766`. |

## See also

- [`examples/livekit/`](../../livekit/): the underlying reactive adapter.
- [`packages/saa-proactive-py/README.md`](../../../packages/saa-proactive-py/README.md): the lifecycle wrapper package.
- [`packages/saa-py/README.md`](../../../packages/saa-py/README.md): SDK reference.


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
