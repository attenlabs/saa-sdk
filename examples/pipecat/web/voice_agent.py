"""SAA-gated OpenAI Realtime voice agent for the web demo.

One-key talkback. Cascaded variant lives in `../voice_agent_cascaded/`.

Turn-taking is owned by SAA, not by OpenAI:

- OpenAI's server-side VAD/turn detection is DISABLED
- Mic audio is fed to the model only while SAA reports class 2 (talking-to-device)
- A response is committed + requested only when SAA emits `on_turn_ready`

The result: the bot responds when, and only when, the user addresses it.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import numpy as np
from typing import Optional

from pipecat.transports.daily.transport import DailyTransport, DailyParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask
from pipecat.pipeline.runner import PipelineRunner

from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.services.openai.realtime import events
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.frames.frames import (
    Frame,
    InterruptionTaskFrame,
    LLMMessagesAppendFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)

from saa_pipecat_client import AttentionEngine, AttentionStartupError

logger = logging.getLogger("web.voice_agent")

OPENAI_REALTIME_SAMPLE_RATE = 24000


def _turn_audio_to_24k_b64(pcm16_16k: bytes) -> str:
    """
    SAA turn audio is int16 mono 16 kHz; gpt-realtime needs 24 kHz.
    """
    pcm16 = np.frombuffer(pcm16_16k, dtype=np.int16)
    pcm24 = np.repeat(pcm16, 3)[::2].astype(np.int16)
    return base64.b64encode(pcm24.tobytes()).decode("ascii")


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
        # no mic audio reaches the model until SAA opens the gate
        start_audio_paused=True,
        settings=OpenAIRealtimeLLMService.Settings(
            model=model,
            system_instruction=system_prompt,
            # disable OpenAI's server-side VAD/turn detection
            session_properties=events.SessionProperties(
                audio=events.AudioConfiguration(
                    input=events.AudioInput(turn_detection=False),
                ),
            ),
        ),
    )

    bot_speaking = _BotSpeakingObserver()

    pipeline = Pipeline(
        [
            transport.input(),
            realtime,
            bot_speaking,
            transport.output(),
        ]
    )
    task = PipelineTask(pipeline)

    engine = AttentionEngine(transport, agent_identity=saa_agent_identity)
    engine.bind_task(task)
    bot_speaking.bind_engine(engine)

    @engine.on_turn_ready
    async def _(ev) -> None:
        # SAA finished a device-directed turn, we get the full utterance
        if not ev.audio_pcm16:
            return

        audio_b64 = _turn_audio_to_24k_b64(ev.audio_pcm16)
        logger.info("SAA turn_ready (%.1fs) — injecting turn + requesting response", ev.duration)
        await realtime.send_client_event(
            events.ConversationItemCreateEvent(
                item=events.ConversationItem(
                    type="message",
                    role="user",
                    content=[events.ItemContent(type="input_audio", audio=audio_b64)],
                ),
            )
        )
        await realtime.send_client_event(events.ResponseCreateEvent())

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
