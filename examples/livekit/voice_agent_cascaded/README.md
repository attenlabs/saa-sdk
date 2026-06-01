# voice_agent_cascaded — SAA-gated cascaded LiveKit agent

A stock cascaded voice agent (Silero VAD → Deepgram STT → OpenAI LLM → Cartesia TTS) with **Attention Labs SAA** wired on top. The agent only responds when the user is actually addressing it (class 2), ignores side conversations (class 1) and silence (class 0), barges out cleanly when the user interrupts, and volunteers a check-in when the room goes quiet.

Built for **LiveKit Agents 1.5.x** using the `AgentServer` + `@server.rtc_session()` shape.

## How SAA fits

SAA runs as a **hosted bridge**: `start_attention_session(...)` summons a hidden participant into your LiveKit room that subscribes to the user's audio+video, runs the addressee classifier, and publishes events on the `"saa"` data topic. Your agent consumes those events through `AttentionEngine`. No model weights or media ever touch your process.

The entire integration is four blocks in [`src/agent.py`](./src/agent.py):

| Block | What it does |
|---|---|
| `start_attention_session(...)` | summons the hidden SAA agent into the room |
| `@engine.on_prediction` | the gate — `session.input.set_audio_enabled(p.aligned_class == 2)` so STT only sees device-directed audio |
| `@engine.on_interrupt` | `session.interrupt()` on a confident barge-in during playback |
| `@engine.on_interjection` | `session.generate_reply(...)` when humans go quiet after a side chat |
| `@session.on("agent_state_changed")` | signals `responding_start`/`responding_stop` so SAA knows when *your* agent is speaking |

That last block matters: SAA's interrupt detector only arms while it believes the AI is responding, and its interjection detector is suppressed during playback. Without the `responding_start`/`responding_stop` signal, `on_interrupt` and `on_interjection` won't fire correctly.

## Quickstart

```bash
cd examples/livekit/voice_agent_cascaded
python -m venv .venv && source .venv/bin/activate

pip install -r requirements.txt
# local dev against this repo's copy of the client:
pip install -e ../../../packages/saa-livekit-client

cp .env.example .env   # then fill in the keys
set -a && source .env && set +a

python src/agent.py dev   # dev mode auto-dispatches the agent to new rooms
```

Connect a frontend (the [`web`](../web) sample, or the [LiveKit Agents Playground](https://agents-playground.livekit.io)) to a room and start talking. The agent replies only when you're addressing it.

## Provider keys vs. the inference gateway

This sample uses **direct provider plugins** (`deepgram` / `openai` / `cartesia`), so it needs `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, and `CARTESIA_API_KEY`. To use **LiveKit's inference gateway** instead — billed through LiveKit Cloud, no per-provider keys — swap the three plugin lines in `AgentSession(...)` for model strings:

```python
session = AgentSession(
    vad=ctx.proc.userdata["vad"],
    stt="deepgram/nova-3",
    llm="openai/gpt-4o-mini",
    tts="cartesia/sonic-2",
)
```

## Turning SAA off

Delete the `start_attention_session(...)` call and the `engine` blocks. The agent reverts to a plain LiveKit cascaded assistant that responds to every utterance.

## Cost note

The SAA hosted bridge is billed per session-minute (one session per `start_attention_session` call). STT/LLM/TTS are billed by their respective providers (or LiveKit Cloud, on the gateway path).

## Deploy

`Dockerfile` builds a worker image. Deploy to LiveKit Cloud Agents or run the container behind your own LiveKit server. Requires `LIVEKIT_URL` reachable from where the worker runs, and the SAA broker reachable from the worker.
