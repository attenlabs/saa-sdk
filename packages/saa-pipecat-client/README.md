# saa-pipecat-client

Pipecat / Daily client for the [saa](https://attentionlabs.ai) hosted bridge.

Adds attention-aware gating, barge-in, and proactive interjection to any
Pipecat voice agent running on Daily — including bots deployed to
[Daily Bots](https://docs.dailybots.ai/architecture) and Pipecat Cloud.

The attention processor runs on our backend. Consumers do **not** deploy
ONNX, MediaPipe, Whisper, or any model weights. They do not run our SDK
in their process. They integrate by minting a hidden-bot meeting token,
calling our REST endpoint, and listening for typed events.

## Install

```bash
pip install saa-pipecat-client
```

## Quickstart — existing Pipecat bot

```python
import os, asyncio
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.daily.transport import DailyTransport, DailyParams

from saa_pipecat_client import (
    AttentionEngine, attention_agent_token, start_attention_session,
)


async def main() -> None:
    # 1. Mint a hidden-bot Daily meeting token using YOUR Daily API key.
    #    We never see it.
    agent_token = attention_agent_token(
        daily_api_key=os.environ["DAILY_API_KEY"],
        room_name="sess-xyz",
    )

    # 2. Summon the saa hosted bot into the room.
    session = await start_attention_session(
        api_key=os.environ["SAA_API_KEY"],
        room_url="https://your-org.daily.co/sess-xyz",
        agent_token=agent_token,
        participant_identity="user-omar",
        attention_config={"frames_per_turn": 3, "vad_threshold": 0.5},
    )

    # 3. Stand up your Pipecat pipeline — unchanged from your existing setup.
    transport = DailyTransport(
        "https://your-org.daily.co/sess-xyz",
        your_user_token,
        "Voice Agent",
        DailyParams(
            audio_in_enabled=True,
            video_in_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
        ),
    )

    # 4. Attach the engine. Pass the PipelineTask so upstream actions
    #    (mute, set_threshold, ...) can be queued back to the hidden bot.
    engine = AttentionEngine(transport, agent_identity=session.agent_identity)

    @engine.on_prediction
    def _(p):
        # Gate your STT — only class 2 (talking-to-device) gets through.
        your_llm_gate.set_enabled(p.aligned_class == 2)

    @engine.on_interrupt
    async def _(ev):
        await your_tts.cancel()
        await engine.responding_stop()

    @engine.on_interjection
    async def _(ev):
        await your_tts.say("Want me to help with something?")

    pipeline = Pipeline([transport.input(), stt, llm, tts, transport.output()])
    task = PipelineTask(pipeline)
    engine.bind_task(task)
    await engine.start()

    runner = PipelineRunner()
    try:
        await runner.run(task)
    finally:
        await engine.stop()
        await session.stop()


if __name__ == "__main__":
    asyncio.run(main())
```

That's the full integration. Works with any Pipecat pipeline, cascaded
or speech-to-speech.

## Greenfield — `build_attention_runner`

For new voice agents:

```python
from saa_pipecat_client import build_attention_runner, TurnReadyEvent

async def handle_turn(event: TurnReadyEvent, transport):
    response_pcm = await my_llm.respond(event.audio_pcm16, frames=event.frames)
    await publish_response_audio(transport, response_pcm)

run = build_attention_runner(on_turn=handle_turn)
# pass `transport` and `task` you built; the factory mints the token,
# starts the session, and wires the engine.
engine = await run(room_url, room_name, human_identity, transport, task)
```

Environment: `SAA_API_KEY`, `DAILY_API_KEY`.

## Event types

| Event | Fires | Payload |
|---|---|---|
| `PredictionEvent` | every 250 ms | `raw_class`, `aligned_class` (0/1/2), `confidence`, `source`, `num_faces` |
| `VADEvent` | every 250 ms | `is_speech`, `probability` |
| listening_start / listening_cancelled | state edges | — |
| `TurnReadyEvent` | end of user turn | `audio_pcm16`, `duration`, `frames`, `context` |
| `InterruptEvent` | user barges in during AI playback | `confidence` |
| `InterjectionEvent` | humans went quiet after side-chat | `reason`, `audio_pcm16`, `duration` |
| `ErrorEvent` | out-of-band errors | `code`, `message` |

Classes: `0`=silent, `1`=human-to-human, `2`=human-to-device.

## Upstream actions

```python
await engine.mute()                  # stop feeding mic into the hosted processor
await engine.unmute()
await engine.responding_start()      # AI is now speaking
await engine.responding_stop()
await engine.set_threshold(0.65)     # model class-2 confidence threshold
```

Each call constructs a `DailyOutputTransportMessageUrgentFrame` addressed
to the hidden bot's `participant_id` and queues it onto the bound
`PipelineTask`. Pipecat's `DailyTransport` does **not** expose a public
`send_app_message()`; the frame-queue path is the only supported send
mechanism. Calls issued before the bot has joined are buffered and
flushed once its participant id resolves.

## Data plane

JSON envelopes on the Daily app-message topic `"saa"` — same shapes as
[`saa-livekit-client`](https://pypi.org/project/saa-livekit-client/), so
a single consumer-side event handler can serve both transports:

| Type | Direction | Carries |
|---|---|---|
| `started` | down | bot online |
| `prediction` | down (4 Hz) | `class`, `aligned_class`, `confidence`, `source`, `num_faces` |
| `vad` | down (4 Hz) | `is_speech`, `probability` |
| `state` | down (edge) | `state` ∈ {`listening`, `cancelled`} |
| `turn_ready` / `interjection` | down (edge) | envelope: `stream_id`, `total_chunks`, `byte_len`, `duration`, `context`, … |
| `turn_chunk` | down | `stream_id`, `index`, `data_base64` (base64-chunked binary PCM + JPEGs) |
| `interrupt` | down (edge) | `confidence` |
| `error` | down | `code`, `message` |
| `mute` / `unmute` / `responding_start` / `responding_stop` / `set_threshold` | up | scoped to `participant_id=agent_pid` |

Binary turn payload (PCM + JPEGs) follows the same layout as the
LiveKit byte-stream variant — see `_wire.py`. Chunk reassembly is handled
inside `AttentionEngine`; consumers see typed events only.

## Daily Bots compatibility

`saa-pipecat-client` is a pure pip dependency, so any Pipecat pipeline that
runs locally also runs on [Daily Bots](https://docs.dailybots.ai/architecture)
and Pipecat Cloud. No extra deployment knobs.

## Requirements

- Python 3.10+
- `pipecat-ai[daily] >= 1.0.0`
- `daily-python >= 0.19.0`
- Daily room URL must be publicly reachable from our cloud (no private VPC)
- Audio + video tracks must both be available (the model is multimodal)
- Customer voice agent and hosted attention bot share the same Daily room

## Docs

- Integration spec: [docs/daily-integration.md](https://github.com/attenlabs/saa-sdk/blob/main/docs/daily-integration.md)
- Example bot: [`examples/pipecat/voice_agent/`](https://github.com/attenlabs/saa-sdk/tree/main/examples/pipecat/voice_agent)

## License

Apache-2.0. © Attention Labs.
