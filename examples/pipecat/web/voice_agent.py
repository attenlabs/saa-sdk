"""SAA-gated OpenAI Realtime voice agent for the web demo.

One-key talkback. Cascaded variant lives in `../voice_agent_cascaded/`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pipecat.transports.daily.transport import DailyTransport, DailyParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask
from pipecat.pipeline.runner import PipelineRunner

from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterruptionTaskFrame,
    LLMMessagesAppendFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)

from saa_pipecat_client import AttentionEngine, AttentionStartupError

logger = logging.getLogger("web.voice_agent")

OPENAI_REALTIME_SAMPLE_RATE = 24000


class _AddresseeGate(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.suppressed = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if self.suppressed and isinstance(frame, InputAudioRawFrame):
            return
        await self.push_frame(frame, direction)


class _BotSpeakingObserver(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self._engine: Optional[AttentionEngine] = None

    def bind_engine(self, engine: AttentionEngine) -> None:
        self._engine = engine

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if self._engine is not None:
            if isinstance(frame, TTSStartedFrame):
                asyncio.create_task(self._engine.responding_start())
            elif isinstance(frame, TTSStoppedFrame):
                asyncio.create_task(self._engine.responding_stop())
        await self.push_frame(frame, direction)


async def run_voice_agent(
    *,
    room_url: str,
    bot_token: str,
    saa_agent_identity: str,
    openai_api_key: str,
    model: str = "gpt-realtime-2",
    system_prompt: str = "You are a helpful voice assistant. Keep replies short and natural.",
) -> None:
    transport = DailyTransport(
        room_url,
        bot_token,
        "SAA Voice Agent",
        DailyParams(
            audio_in_enabled=True,
            audio_in_user_tracks=True,
            video_in_enabled=True,
            audio_in_sample_rate=OPENAI_REALTIME_SAMPLE_RATE,
            audio_out_enabled=True,
            audio_out_sample_rate=OPENAI_REALTIME_SAMPLE_RATE,
        ),
    )

    realtime = OpenAIRealtimeLLMService(
        api_key=openai_api_key,
        settings=OpenAIRealtimeLLMService.Settings(
            model=model,
            system_instruction=system_prompt,
        ),
    )

    addressee_gate = _AddresseeGate()
    bot_speaking = _BotSpeakingObserver()

    pipeline = Pipeline(
        [
            transport.input(),
            addressee_gate,
            realtime,
            bot_speaking,
            transport.output(),
        ]
    )
    task = PipelineTask(pipeline)

    engine = AttentionEngine(transport, agent_identity=saa_agent_identity)
    engine.bind_task(task)
    bot_speaking.bind_engine(engine)

    @engine.on_prediction
    def _(p) -> None:
        addressee_gate.suppressed = p.aligned_class == 1 and p.confidence > 0.7

    @engine.on_interrupt
    async def _(ev) -> None:
        logger.info("SAA interrupt fired (confidence=%.2f)", ev.confidence)
        await task.queue_frames([InterruptionTaskFrame()])
        await engine.responding_stop()

    @engine.on_interjection
    async def _(ev) -> None:
        logger.info("SAA interjection fired (reason=%s)", ev.reason)
        await task.queue_frames(
            [
                LLMMessagesAppendFrame(
                    messages=[
                        {
                            "role": "system",
                            "content": "Briefly offer to help in one short sentence.",
                        }
                    ],
                    run_llm=True,
                ),
            ]
        )

    @engine.on_error
    def _(ev) -> None:
        logger.warning("SAA error [%s]: %s", ev.code, ev.message)

    @transport.event_handler("on_first_participant_joined")
    async def _on_first_participant_joined(transport_, participant):
        # engine.start() waits for the bot's "started" handshake. It now fails
        # fast with AttentionStartupError if the bot publishes an error first,
        # or TimeoutError if the bot never reports in — either way tear the
        # agent down cleanly instead of leaking an uncaught event-handler exc.
        try:
            await engine.start()
        except AttentionStartupError as e:
            logger.error("SAA bot failed to start (%s) — stopping voice agent", e)
            await task.cancel()
            return
        except asyncio.TimeoutError:
            logger.error(
                "SAA bot never published 'started' — stopping voice agent for room=%s",
                room_url,
            )
            await task.cancel()
            return
        logger.info(
            "SAA gating active for room=%s (saa_agent=%s, model=%s)",
            room_url, saa_agent_identity, model,
        )

    @transport.event_handler("on_participant_left")
    async def _on_participant_left(transport_, participant, reason):
        user_name = participant.get("info", {}).get("userName")
        if user_name and user_name.startswith("user-"):
            logger.info(
                "human %s left (%s); shutting down voice agent for room=%s",
                user_name, reason, room_url,
            )
            try:
                await engine.stop()
            except Exception:
                logger.exception("engine.stop failed")
            await task.cancel()

    runner = PipelineRunner()
    try:
        await runner.run(task)
    finally:
        try:
            await engine.stop()
        except Exception:
            pass
