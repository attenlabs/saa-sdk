# SAA + LiveKit Agents

Reference samples that add **attention labs SAA** device-directed gating to LiveKit voice agents. SAA decides, per utterance, whether speech in the room was meant for the agent or not.

## How SAA integrates

1. Your agent calls `start_attention_session(...)` to start a SAA session for your room.
2. SAA joins your LiveKit room, subscribes to the user's audio and video, and runs the classifier.
3. It publishes events (`prediction`, `vad`, `turn_ready`, `interrupt`, `interjection`) on the `"saa"` LiveKit data topic.
4. Your agent consumes them via `AttentionEngine` and gates the session.

The whole client ([`packages/saa-livekit-client`](../../packages/saa-livekit-client)) is ~50 KB of pure Python.

## Samples

| Sample | Stack | Run |
|---|---|---|
| [`voice_agent_realtime/`](./voice_agent_realtime) | OpenAI Realtime (speech-to-speech), + SAA | `python agent.py dev` |
| [`web/`](./web) | Vanilla HTML + `livekit-client` browser client rendering the prediction overlay | `python -m uvicorn token_server:app` |

All target **LiveKit Agents 1.5.x** using the `AgentServer` + `@server.rtc_session()` shape. (`WorkerOptions(entrypoint_fnc=...)` also works on 1.5.x and is the older idiom.)

## Quick start: realtime agent + web client

```bash
git clone https://github.com/attenlabs/saa-sdk.git
cd saa-sdk/examples/livekit
```

Talk to a SAA-gated OpenAI Realtime agent in your browser. Two terminals, one shared `.env`. Needs **Python 3.10+**.

```bash
cd examples/livekit
cp .env.example .env     # fill LIVEKIT_*, SAA_API_KEY, OPENAI_API_KEY (see Shared environment)
```

The samples **auto-load** this `.env`, so the commands below are identical on Windows, macOS, and Linux, no shell `source` step.

**Terminal 1, the realtime voice agent** (owns SAA, auto-joins new rooms):

```bash
cd examples/livekit/voice_agent_realtime
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ../../../packages/saa-livekit-client
pip install -r requirements.txt
python agent.py dev
```

**Terminal 2, the web client** (mints a join token, renders the overlay):

```bash
cd examples/livekit/web
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn token_server:app --port 8000
```

Open <http://localhost:8000> and click **Start**. The browser creates a room, the realtime agent auto-joins and summons SAA. The layout will only go green when you're talking to the device.

> Start the agent **before** clicking Start, so it's registered for the room dispatch. Both halves use the same LiveKit project by construction (the one shared `.env`). `python -m uvicorn` (not bare `uvicorn`) ensures the venv's interpreter is used.

## Shared environment

All samples read **one** env file, [`.env`](./.env.example) in this directory, so `LIVEKIT_*` (which the browser and the agent must share) lives in exactly one place and can't drift.

```bash
cd examples/livekit
cp .env.example .env          # then fill it in once
```

It holds the union of every sample's keys, grouped by which sample needs them:

| Key | Used by |
|---|---|
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | the agent and the web client, must be the **same** project |
| `SAA_API_KEY` | the voice agent (the web token server doesn't summon SAA) |
| `OPENAI_API_KEY` | realtime (the model) |

Each sample **auto-loads** this file (via `python-dotenv`) from `examples/livekit/.env`, no shell `source` needed, so setup is identical on Windows, macOS, and Linux. Anything you've already exported (or that Docker/CI injects) takes precedence over the file.

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

Plus a `@session.on("agent_state_changed")` hook that calls `engine.responding_start()` / `responding_stop()` so SAA knows when your agent is the one speaking, required for interrupt and interjection to fire correctly.

## Requirements & limitations

- The agent's LiveKit URL must be reachable from the SAA cloud.
- Both audio **and** video tracks should be available.
- One target participant per session. Multi-user rooms need one `start_attention_session` call each.

## Recommended usage

Try three send thresholds and keep the one that performs best: `0.6`, `0.77`, `0.88`.
Raise it for fewer false triggers, lower it to catch borderline speech. Set it on the engine with `attention.set_threshold(v)`.
