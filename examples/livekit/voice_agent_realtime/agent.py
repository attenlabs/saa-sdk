# SAA-driven realtime (speech-to-speech) voice agent for LiveKit Agents 1.5.x+
# OpenAI Realtime with server VAD disabled; SAA is the turn-taker and injects each
# device-directed turn into the model (push_audio -> commit_audio -> generate_reply)
import asyncio
import logging
import os
import time
from logging.handlers import RotatingFileHandler

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli
from livekit.agents.voice import room_io
from livekit.plugins import openai, silero

from saa_livekit_client import (
    AttentionEngine,
    attention_agent_token,
    start_attention_session,
)
from saa_livekit_client.agents import inject_realtime_turn

from pathlib import Path
from dotenv import load_dotenv

# auto-load the shared examples/livekit/.env
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

logger = logging.getLogger("voice-agent-realtime")

# per-run log file at DEBUG; also captures livekit-agents + openai plugin internals
_fh = RotatingFileHandler(f"saa-agent-{int(time.time())}.log", maxBytes=5_000_000, backupCount=3)
_fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
_fh.setLevel(logging.DEBUG)
for _name in ("voice-agent-realtime", "livekit", "saa_livekit_client"):
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.DEBUG)
    _lg.addHandler(_fh)

INTERJECTION_INSTRUCTIONS = "The user went quiet. Briefly check in or offer help based on what they were just discussing."
FOLLOWUP_INSTRUCTIONS = "Respond to the user's reply. If they dismissed you, acknowledge briefly and stop."


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a helpful voice assistant. Be brief")


def _prewarm(proc) -> None:
    proc.userdata["vad"] = silero.VAD.load()


server = AgentServer(setup_fnc=_prewarm)


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    user = await ctx.wait_for_participant()

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
        attention_config={"frames_per_turn": 0},
    )
    ctx.add_shutdown_callback(saa.stop)

    # speech-to-speech -> SAA is the turn-taker: server VAD off
    session = AgentSession(
        llm=openai.realtime.RealtimeModel(voice="alloy", turn_detection=None),
        vad=ctx.proc.userdata["vad"],
        turn_detection="manual",
    )

    await session.start(
        agent=Assistant(),
        room=ctx.room,
        room_options=room_io.RoomOptions(video_input=True),
    )
    session.input.set_audio_enabled(False)  # model hears only SAA-injected turns

    engine = AttentionEngine(ctx.room, agent_identity=saa.agent_identity)
    ctx.add_shutdown_callback(engine.stop)

    @engine.on_turn_ready
    def _(ev) -> None:
        logger.info("turn_ready dur=%.2f context=%s", ev.duration, ev.context)
        instr = FOLLOWUP_INSTRUCTIONS if ev.context == "interjection_follow_up" else None
        if not inject_realtime_turn(session, ev, instructions=instr):
            logger.warning("no realtime session — dropped turn")

    @engine.on_interrupt
    def _(ev) -> None:
        logger.info("interrupt conf=%.3f", ev.confidence)
        session.interrupt()

    @engine.on_interjection
    def _(ev) -> None:
        logger.info("interjection reason=%s", ev.reason)
        if not inject_realtime_turn(session, ev, instructions=INTERJECTION_INSTRUCTIONS):
            logger.warning("no realtime session — dropped interjection")

    # tell SAA when our agent is speaking — arms interrupt, suppresses interjection
    @session.on("agent_state_changed")
    def _(ev) -> None:
        logger.info("agent_state %s->%s", ev.old_state, ev.new_state)
        if ev.new_state == "speaking":
            asyncio.create_task(engine.responding_start())
        elif ev.old_state == "speaking":
            asyncio.create_task(engine.responding_stop())

    @session.on("user_state_changed")
    def _(ev) -> None:
        logger.info("user_state %s->%s", ev.old_state, ev.new_state)

    @session.on("user_input_transcribed")
    def _(ev) -> None:
        logger.info("transcribed final=%s %r", ev.is_final, ev.transcript)

    @session.on("conversation_item_added")
    def _(ev) -> None:
        logger.info("item_added role=%s", getattr(ev.item, "role", "?"))

    @session.on("speech_created")
    def _(ev) -> None:
        logger.info("speech_created source=%s", ev.source)

    @session.on("error")
    def _(ev) -> None:
        logger.error("session error src=%s err=%r", type(ev.source).__name__, ev.error)

    await engine.start()
    logger.info("SAA gating active (agent=%s)", saa.agent_identity)


if __name__ == "__main__":
    cli.run_app(server)
