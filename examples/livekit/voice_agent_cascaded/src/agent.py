# SAA-gated cascaded voice agent for LiveKit Agents 1.5.x
# Silero VAD -> Deepgram STT -> OpenAI LLM -> Cartesia TTS, gated by Attention Labs SAA
# The SAA-specific code is the start_attention_session call + the @engine.on_* blocks
import asyncio
import logging
import os

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli
from livekit.agents.voice import room_io
from livekit.plugins import cartesia, deepgram, openai, silero

from saa_livekit_client import (
    AttentionEngine,
    attention_agent_token,
    start_attention_session,
)

logger = logging.getLogger("voice-agent-cascaded")


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions="You are a helpful voice assistant. Keep replies short and natural",
        )


def _prewarm(proc) -> None:
    # load Silero once per worker process, shared across sessions
    proc.userdata["vad"] = silero.VAD.load()


server = AgentServer(setup_fnc=_prewarm)


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    user = await ctx.wait_for_participant()

    # summon the hidden SAA agent — it subscribes to the user's audio+video and
    # publishes addressee predictions on the "saa" data topic, never any media
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

    # stock cascaded pipeline
    # to use LiveKit's inference gateway instead of direct plugins, swap the
    # three lines below for: stt="deepgram/nova-3", llm="openai/gpt-4o-mini",
    # tts="cartesia/sonic-2" (no provider keys needed, billed via LiveKit Cloud)
    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(model="nova-3"),
        llm=openai.LLM(model="gpt-4o-mini"),
        tts=cartesia.TTS(model="sonic-2"),
    )

    # start the session before wiring SAA — session.input and session.interrupt()
    # are only valid once it's running, and predictions arrive the moment the
    # engine starts
    await session.start(
        agent=Assistant(),
        room=ctx.room,
        room_options=room_io.RoomOptions(video_input=True),
    )

    engine = AttentionEngine(ctx.room, agent_identity=saa.agent_identity)
    ctx.add_shutdown_callback(engine.stop)

    @engine.on_prediction
    def _(p) -> None:
        # the gate: STT only sees audio the user directed at the device (class 2)
        session.input.set_audio_enabled(p.aligned_class == 2)

    @engine.on_interrupt
    def _(ev) -> None:
        # confident barge-in during playback — cancel TTS + the in-flight LLM turn
        session.interrupt()

    @engine.on_interjection
    async def _(ev) -> None:
        # humans went quiet after a side chat — volunteer a one-line check-in
        await session.generate_reply(instructions="Briefly offer to help in one short sentence")

    # tell SAA when our agent is the one speaking — gates its predictions, arms
    # the interrupt detector, and suppresses interjections during playback
    @session.on("agent_state_changed")
    def _(ev) -> None:
        if ev.new_state == "speaking":
            asyncio.create_task(engine.responding_start())
        elif ev.old_state == "speaking":
            asyncio.create_task(engine.responding_stop())

    await engine.start()
    logger.info("SAA gating active (agent=%s)", saa.agent_identity)


if __name__ == "__main__":
    cli.run_app(server)
