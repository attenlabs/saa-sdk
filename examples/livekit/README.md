# SAA + LiveKit Agents

Reference samples that add **Attention Labs SAA** addressee gating to LiveKit voice agents. SAA decides, per utterance, whether speech in the room was meant for the agent — so your STT / LLM / TTS only run on audio the user actually directed at the device.

## The integration shape — hosted bridge

SAA integrates with LiveKit as a **hosted bridge**, not an in-process plugin:

1. Your agent calls `start_attention_session(...)`, which POSTs to the SAA broker.
2. A **hidden participant** joins your LiveKit room, subscribes to the user's audio+video, and runs the classifier on Attention Labs' infrastructure.
3. It publishes events (`prediction`, `vad`, `turn_ready`, `interrupt`, `interjection`) on the `"saa"` LiveKit data topic.
4. Your agent consumes them via `AttentionEngine` and gates the session.

No model weights, no ML dependencies, and no media ever enter your process — the client ([`packages/saa-livekit-client`](../../packages/saa-livekit-client)) is ~50 KB of pure Python.

## Samples

| Sample | Stack | Run |
|---|---|---|
| [`voice_agent_cascaded/`](./voice_agent_cascaded) | Silero VAD → Deepgram STT → OpenAI LLM → Cartesia TTS, SAA-gated | `python src/agent.py dev` |
| [`voice_agent_realtime/`](./voice_agent_realtime) | OpenAI Realtime (speech-to-speech), SAA-gated — the case stock LiveKit can't gate | `python agent.py dev` |
| [`web/`](./web) | Vanilla HTML + `livekit-client` browser client rendering the prediction overlay | `uvicorn token_server:app` |

All target **LiveKit Agents 1.5.x** using the `AgentServer` + `@server.rtc_session()` shape. (`WorkerOptions(entrypoint_fnc=...)` also works on 1.5.x and is the older idiom.)

## Quick start — realtime agent + web client

Talk to a SAA-gated OpenAI Realtime agent in your browser. Two terminals, one shared `.env`.

```bash
cd examples/livekit
cp .env.example .env     # fill LIVEKIT_*, SAA_API_KEY, OPENAI_API_KEY (see Shared environment)
```

**Terminal 1 — the realtime voice agent** (owns SAA, auto-joins new rooms):

```bash
cd examples/livekit/voice_agent_realtime
pip install -e ../../../packages/saa-livekit-client
pip install -r requirements.txt
set -a && source ../.env && set +a
python agent.py dev
```

**Terminal 2 — the web client** (mints a join token, renders the overlay):

```bash
cd examples/livekit/web
pip install -r requirements.txt
set -a && source ../.env && set +a
uvicorn token_server:app --port 8000
```

Open <http://localhost:8000> and click **Start**. The browser creates a room, the realtime agent auto-joins and summons SAA, and you're talking — it answers only when you address it, and the pill goes green at exactly those moments. Status shows `waiting for agent…` until the agent's audio arrives.

> Start the agent **before** clicking Start, so it's registered for the room dispatch. Both halves use the same LiveKit project by construction (the one shared `.env`).

## Shared environment

All samples read **one** env file — [`.env`](./.env.example) in this directory — so `LIVEKIT_*` (which the browser and the agent must share) lives in exactly one place and can't drift.

```bash
cd examples/livekit
cp .env.example .env          # then fill it in once
```

It holds the union of every sample's keys, grouped by which sample needs them:

| Key | Used by |
|---|---|
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | all three — must be the **same** project |
| `SAA_API_KEY` | the voice agents (the web token server doesn't summon SAA) |
| `OPENAI_API_KEY` | realtime (the model) + cascaded (the LLM) |
| `DEEPGRAM_API_KEY` / `CARTESIA_API_KEY` | cascaded only (STT/TTS), unless on the inference gateway |

Load it from a sample dir before running:

```bash
set -a && source ../.env && set +a
```

## The five lines that integrate SAA

```python
saa = await start_attention_session(api_key=..., livekit_url=..., agent_token=..., room_name=..., participant_identity=...)
engine = AttentionEngine(ctx.room, agent_identity=saa.agent_identity)

@engine.on_prediction
def _(p): session.input.set_audio_enabled(p.aligned_class == 2)   # the gate

@engine.on_interrupt
def _(ev): session.interrupt()                                    # barge-in

@engine.on_interjection
async def _(ev): await session.generate_reply(instructions="...")  # proactive
```

Plus a `@session.on("agent_state_changed")` hook that calls `engine.responding_start()` / `responding_stop()` so SAA knows when your agent is the one speaking — required for interrupt and interjection to fire correctly.

## Requirements & limitations

- The agent's LiveKit URL must be reachable from the SAA cloud.
- Both audio **and** video tracks should be available.
- One target participant per session. Multi-user rooms need one `start_attention_session` call each.
