# saa-livekit-client

LiveKit client for the [saa](https://attentionlabs.ai).

Adds attention-aware gating, barge-in, and proactive interjection to any LiveKit voice agent.

## Install

```bash
pip install saa-livekit-client
```

## Quickstart: existing voice agent

```python
import asyncio
import os

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli
from livekit.plugins import openai
from saa_livekit_client import (
    AttentionEngine, attention_agent_token, start_attention_session,
)


class MyAssistant(Agent):
    def __init__(self):
        super().__init__(instructions="You are a helpful assistant")


server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext):
    await ctx.connect()
    user = await ctx.wait_for_participant()

    # summon the saa hosted agent into the room
    saa = await start_attention_session(
        api_key=os.environ["SAA_API_KEY"],
        livekit_url=os.environ["LIVEKIT_URL"],
        agent_token=attention_agent_token(
            api_key=os.environ["LIVEKIT_API_KEY"],
            api_secret=os.environ["LIVEKIT_API_SECRET"],
            room_name=ctx.room.name,
        ),
        room_name=ctx.room.name,
        participant_identity=user.identity,
    )
    ctx.add_shutdown_callback(saa.stop)

    # stand up the voice agent, then wire saa on top of the running session
    # (session.input / session.interrupt() are only valid once started)
    voice = AgentSession(llm=openai.realtime.RealtimeModel(voice="alloy"))
    await voice.start(agent=MyAssistant(), room=ctx.room)

    engine = AttentionEngine(ctx.room, agent_identity=saa.agent_identity)
    ctx.add_shutdown_callback(engine.stop)

    @engine.on_prediction
    def _(p):
        # gate the mic, only class 2 (talking-to-device) reaches the model
        voice.input.set_audio_enabled(p.aligned_class == 2)

    @engine.on_interrupt
    def _(ev):
        voice.interrupt()

    @engine.on_interjection
    async def _(ev):
        await voice.generate_reply(instructions="Briefly offer to help")

    # tell saa when the agent is speaking, arms interrupt, suppresses interjection
    @voice.on("agent_state_changed")
    def _(ev):
        if ev.new_state == "speaking":
            asyncio.create_task(engine.responding_start())
        elif ev.old_state == "speaking":
            asyncio.create_task(engine.responding_stop())

    await engine.start()


if __name__ == "__main__":
    cli.run_app(server)
```

That's the whole integration. Works with any LiveKit pipeline, including `RealtimeModel`
speech-to-speech. Runnable variants are in the [`examples/livekit/`](https://github.com/attenlabs/saa-sdk/tree/main/examples/livekit)
samples. (`WorkerOptions(entrypoint_fnc=...)` also works on 1.5.x, the older idiom.)

## Greenfield, `build_attention_entrypoint`

For new voice agents that don't have an existing pipeline:

```python
from livekit.agents import AgentServer, JobContext, cli
from saa_livekit_client import build_attention_entrypoint, TurnReadyEvent

async def handle_turn(event: TurnReadyEvent, ctx: JobContext):
    # event.audio_pcm16 = int16 mono 16 kHz; event.frames = list[TurnFrame]
    response_pcm = await my_llm.respond(event.audio_pcm16, frames=event.frames)
    await publish_response_audio(ctx.room, response_pcm)

entrypoint = build_attention_entrypoint(on_turn=handle_turn)

server = AgentServer()
server.rtc_session()(entrypoint)
# or the older idiom: cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))

if __name__ == "__main__":
    cli.run_app(server)
```

Environment: `SAA_API_KEY`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `LIVEKIT_URL`.

## Event types

| Event | Fires | Payload |
|---|---|---|
| `PredictionEvent` | every 250 ms | `raw_class`, `aligned_class` (0/1/2), `confidence`, `source`, `num_faces`, `responding` |
| `VADEvent` | every 250 ms | `is_speech`, `probability` |
| warmup | model warmed up, predictions begin | none |
| listening_start / listening_cancelled | state edges | none |
| `TurnReadyEvent` | end of user turn | `audio_pcm16`, `duration`, `frames`, `context` |
| `InterruptEvent` | user barges in during AI playback | `confidence` |
| `InterjectionEvent` | humans went quiet after side-chat | `reason`, `audio_pcm16`, `duration` |
| `ErrorEvent` | out-of-band errors | `code`, `message` |

Classes: `0`=silent, `1`=human-to-human, `2`=human-to-device. `responding` is `True` while the AI is mid-playback.

Each is delivered through an `@engine.on_*` callback: `on_prediction`, `on_vad`, `on_warmup`, `on_listening_start`, `on_listening_cancelled`, `on_turn_ready`, `on_interrupt`, `on_interjection`, `on_error`.

## Upstream actions

```python
await attention.mute()                       # stop feeding mic to processor
await attention.unmute()
await attention.responding_start()           # AI is now speaking
await attention.responding_stop()
await attention.set_threshold(0.65)          # model class-2 confidence threshold
```

These are routed only to the SAA agent (`destination_identities=[...]`)
so they never leak to other room participants.

## Requirements

- Python 3.10+
- LiveKit URL must be publicly reachable from our cloud (no private VPC)
- Audio + video tracks must both be available (the model is multimodal)
- Customer voice agent and hosted attention agent share the same LiveKit room

## License

Apache-2.0. © Attention Labs.
