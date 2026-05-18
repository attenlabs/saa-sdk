"""End-to-end voice agent: SAA → Pipecat (STT → LLM → TTS).

The SAA gate sits between ``transport.input()`` and Deepgram. SAA decides
which audio is addressed to the device; only that audio reaches STT, so
STT credits and LLM tokens are spent only on directed speech.

The gate also forwards upstream video (when the transport ships it) to
SAA, surfacing gaze + face-presence signal in the prediction stream.
Live decisions are mirrored as ``SAADecisionFrame`` sidecars; pair with
``overlay_server.py`` to drive an ``@attenlabs/saa-overlay`` dashboard.

Run against a Pipecat cloud transport via:

    cp .env.example .env && $EDITOR .env
    pip install -r requirements.txt

    pipecat-runner --transport daily       bot:bot
    pipecat-runner --transport smallwebrtc bot:bot
    pipecat-runner --transport twilio      bot:bot   # / telnyx / plivo

Or on a laptop in legacy local-mic mode (SAA owns the microphone;
Pipecat's local transport does not capture):

    python bot.py
"""
from __future__ import annotations

import os
import sys
from typing import Any, Callable, Optional

from dotenv import load_dotenv
from loguru import logger

from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.daily.transport import DailyParams
from pipecat.transports.local.audio import (
    LocalAudioTransport,
    LocalAudioTransportParams,
)

from saa_gate import SAA_SAMPLE_RATE, SAAGate

load_dotenv(override=True)

SYSTEM_PROMPT = (
    "You are a helpful voice assistant in a Pipecat pipeline that has SAA "
    "in front of you. Because of SAA you only ever hear speech that was "
    "directed at you, don't second-guess whether the user is talking to "
    "someone else, and don't acknowledge background audio. Keep replies "
    "under three sentences. Don't use markdown."
)


def _require_env(*keys: str) -> None:
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        logger.error("Missing required env vars: {}", ", ".join(missing))
        logger.error("Copy .env.example to .env and fill in values, then re-run.")
        sys.exit(2)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _build_gate(*, upstream_mode: bool) -> SAAGate:
    return SAAGate(
        token=os.environ["ATTENLABS_TOKEN"],
        url=os.environ.get("ATTENLABS_URL") or None,
        threshold=float(os.environ.get("SAA_THRESHOLD", "0.7")),
        upstream_mode=upstream_mode,
        forward_upstream_video=(
            upstream_mode and _env_bool("SAA_FORWARD_VIDEO", True)
        ),
        enable_barge_in=_env_bool("SAA_BARGE_IN", True),
        suppress_during_bot_speech=True,
        emit_user_speaking_frames=True,
        emit_sidecar_frames=_env_bool("SAA_EMIT_SIDECAR", True),
        passthrough_during_warmup=_env_bool("SAA_PASSTHROUGH_WARMUP", False),
        enable_video=_env_bool("SAA_ENABLE_VIDEO", True) if not upstream_mode else False,
    )


async def run_bot(
    transport: BaseTransport,
    runner_args: RunnerArguments,
    *,
    upstream_mode: bool = True,
    decision_listener: Optional[Callable[..., Any]] = None,
) -> None:
    _require_env(
        "ATTENLABS_TOKEN",
        "DEEPGRAM_API_KEY",
        "OPENAI_API_KEY",
        "CARTESIA_API_KEY",
    )

    saa = _build_gate(upstream_mode=upstream_mode)
    if decision_listener is not None:
        saa.add_decision_listener(decision_listener)

    stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])

    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        settings=CartesiaTTSService.Settings(
            voice=os.environ.get(
                "CARTESIA_VOICE_ID",
                "71a7ad14-091c-4e8e-a314-022ece01c121",  # British Reading Lady
            ),
        ),
    )

    llm = OpenAILLMService(
        api_key=os.environ["OPENAI_API_KEY"],
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        settings=OpenAILLMService.Settings(system_instruction=SYSTEM_PROMPT),
    )

    context = LLMContext()
    # SAA emits UserStarted/UserStoppedSpeakingFrame around each gated
    # utterance, so the user-side aggregator does not need its own VAD.
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=None),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            saa,                     # ← only device-directed audio passes
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=SAA_SAMPLE_RATE,
        ),
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )

    @transport.event_handler("on_client_connected")
    async def _on_connected(transport, client):  # noqa: ARG001
        logger.info("client connected, introducing the bot")
        context.add_message(
            {"role": "developer", "content": "Briefly introduce yourself."}
        )
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(transport, client):  # noqa: ARG001
        logger.info("client disconnected")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    await runner.run(task)


transport_params = {
    "daily": lambda: DailyParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=SAA_SAMPLE_RATE,
        video_in_enabled=_env_bool("SAA_FORWARD_VIDEO", True),
    ),
    "smallwebrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=SAA_SAMPLE_RATE,
        video_in_enabled=_env_bool("SAA_FORWARD_VIDEO", True),
    ),
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=SAA_SAMPLE_RATE,
        video_in_enabled=_env_bool("SAA_FORWARD_VIDEO", True),
    ),
    "twilio": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=SAA_SAMPLE_RATE,
        audio_out_sample_rate=SAA_SAMPLE_RATE,
    ),
    "telnyx": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=SAA_SAMPLE_RATE,
        audio_out_sample_rate=SAA_SAMPLE_RATE,
    ),
    "plivo": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=SAA_SAMPLE_RATE,
        audio_out_sample_rate=SAA_SAMPLE_RATE,
    ),
}


async def bot(runner_args: RunnerArguments) -> None:
    """Pipecat-runner entrypoint (every cloud transport, upstream mode)."""
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args, upstream_mode=True)


def _local_transport() -> LocalAudioTransport:
    """Build a LocalAudioTransport with mic capture disabled (SAA owns it)."""
    return LocalAudioTransport(
        params=LocalAudioTransportParams(
            audio_in_enabled=False,
            audio_out_enabled=True,
            audio_in_sample_rate=SAA_SAMPLE_RATE,
            audio_out_sample_rate=24_000,
        )
    )


async def _main_local() -> None:
    transport = _local_transport()
    runner_args = RunnerArguments(handle_sigint=True)
    logger.info(
        "starting local-audio bot (legacy local-mic mode). SAA owns the "
        "microphone; only directed speech reaches STT. Ctrl-C to quit."
    )
    await run_bot(transport, runner_args, upstream_mode=False)


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(_main_local())
    except KeyboardInterrupt:
        logger.info("bye")
