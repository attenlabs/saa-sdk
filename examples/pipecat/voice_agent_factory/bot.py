# SAA-gated Daily bot via the build_attention_runner greenfield factory
import asyncio
import logging
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.daily.transport import DailyParams, DailyTransport

from saa_pipecat_client import build_attention_runner

load_dotenv(Path(__file__).resolve().parent / ".env")

log = logging.getLogger("voice-agent-factory")
logging.basicConfig(level=logging.INFO)

DAILY_API = "https://api.daily.co/v1"


async def handle_turn(ev, transport):
    # one device-directed utterance; forward ev.audio_pcm16 to your STT/LLM/TTS
    log.info("turn ready: %.2fs context=%s", ev.duration, ev.context)


async def _daily_post(path, key, body):
    async with httpx.AsyncClient(timeout=20.0) as http:
        r = await http.post(
            f"{DAILY_API}{path}", headers={"Authorization": f"Bearer {key}"}, json=body
        )
    r.raise_for_status()
    return r.json()


async def main():
    # the factory mints the SAA bot token; this bot still needs its own Daily room + token
    daily_key = os.environ["DAILY_API_KEY"]
    exp = int(time.time()) + 3600
    room = await _daily_post("/rooms", daily_key, {"properties": {"exp": exp}})
    room_url, room_name = room["url"], room["name"]
    human = f"user-{int(time.time())}"
    user_token = (await _daily_post("/meeting-tokens", daily_key,
        {"properties": {"room_name": room_name, "user_name": human, "exp": exp}}))["token"]
    bot_token = (await _daily_post("/meeting-tokens", daily_key,
        {"properties": {"room_name": room_name, "user_name": "SAA Voice Agent", "is_owner": True, "exp": exp}}))["token"]

    log.info("join as the human: %s?t=%s", room_url, user_token)

    transport = DailyTransport(
        room_url, bot_token, "SAA Voice Agent",
        DailyParams(audio_in_enabled=True, audio_in_user_tracks=True, video_in_enabled=True),
    )
    task = PipelineTask(Pipeline([transport.input(), transport.output()]))

    run = build_attention_runner(on_turn=handle_turn)
    engine, session = await run(room_url, room_name, human, transport, task)

    runner = PipelineRunner()
    try:
        await runner.run(task)
    finally:
        await engine.stop()
        await session.stop()


if __name__ == "__main__":
    asyncio.run(main())
