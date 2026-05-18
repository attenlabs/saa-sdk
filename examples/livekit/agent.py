"""SAA-gated LiveKit voice agent, production reference.

Two modes, selected via ``SAA_GATE_MODE``:

* ``pre_stt`` (default): true pre-ASR gate. Participant audio is fed
  to SAA via :class:`SAAAudioBridge` (upstream mode, the SAA SDK does
  not open a local mic). Only audio SAA classifies as device-directed
  (cls=2, conf ≥ threshold) reaches the STT plugin. Saves STT credits,
  LLM tokens, and TTS minutes per suppressed turn.

* ``response``: response gate. STT runs on every turn; the agent skips
  LLM + TTS for turns SAA hasn't classified as device-directed within
  ``SAA_GATE_TTL_S`` seconds via :class:`SAAGate` and
  ``on_user_turn_completed`` + ``StopResponse``. Use this only when you
  cannot override ``Agent.stt_node`` or when SAA must run as a
  separate local-mic capture.

The pre-STT mode is the recommended deployment. It also subscribes to
the participant's camera track and forwards JPEG frames to SAA so the
classifier can use gaze + face presence, multi-modal scoring is what
makes SAA's directed-speech verdict materially better than VAD-only
endpointing.

Run in dev mode::

    cp .env.example .env  # fill in keys
    pip install -e .
    python agent.py dev

Run in console mode (no LiveKit room, useful for local dev)::

    python agent.py console

Deploy::

    docker build -t saa-livekit-agent .
    docker run --env-file .env saa-livekit-agent

Verified against ``livekit-agents`` 1.0.x and ``attenlabs-saa`` 1.0.0.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
from collections.abc import AsyncIterable
from typing import Any, Optional

from dotenv import load_dotenv

from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    ModelSettings,
    RoomInputOptions,
    StopResponse,
    WorkerOptions,
    cli,
    function_tool,
    metrics,
    stt as stt_pkg,
)
from livekit.plugins import deepgram, openai, silero

from saa_gate import (
    SAA_NUM_CHANNELS,
    SAA_SAMPLE_RATE,
    SAAAudioBridge,
    SAAGate,
    SAAGateSnapshot,
)

load_dotenv()

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("saa-livekit-agent")


SYSTEM_PROMPT = (
    "You are a helpful voice assistant on a LiveKit call. Because SAA "
    "runs in front of you, you only ever hear speech that was directed "
    "at the device, don't second-guess whether the user is talking to "
    "someone else. Keep replies under three sentences. Don't use markdown."
)

VALID_MODES = ("pre_stt", "response")
DEFAULT_VIDEO_JPEG_QUALITY = 60
DEFAULT_VIDEO_FPS = 4.0


# ───────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────


def _require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit(
            "Missing required environment variables: "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill in the values."
        )


def _resolve_mode() -> str:
    raw = (os.environ.get("SAA_GATE_MODE") or "pre_stt").strip().lower()
    if raw not in VALID_MODES:
        raise SystemExit(
            f"SAA_GATE_MODE must be one of {VALID_MODES}, got {raw!r}."
        )
    return raw


def _safe_text_preview(msg: Any, *, limit: int = 80) -> str:
    text = getattr(msg, "text_content", None) or getattr(msg, "content", None) or ""
    if not isinstance(text, str):
        text = str(text)
    text = text.strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


# ───────────────────────────────────────────────────────────────────
# Function tools shared by both modes (LLM-driven SAA control)
# ───────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────
# Pre-STT mode: SAAPreSTTAssistant
# ───────────────────────────────────────────────────────────────────


class SAAPreSTTAssistant(Agent):
    """Production pre-ASR gated assistant.

    Overrides :meth:`Agent.stt_node` so the default STT plugin only ever
    sees SAA-blessed audio. The participant's audio is forwarded into
    SAA via :class:`SAAAudioBridge` (upstream mode); SAA's endpointer
    classifies each utterance; only ``speech_ready`` audio is yielded
    downstream for transcription.

    Exposes two function tools so the LLM can adjust SAA at runtime:
    ``set_attention_sensitivity`` and ``mute_microphone`` /
    ``unmute_microphone``. These show up in the system prompt and let
    the user phrase tuning requests naturally ("only respond when I'm
    looking right at you", "mute the microphone").
    """

    def __init__(self, *, bridge: SAAAudioBridge) -> None:
        super().__init__(
            instructions=SYSTEM_PROMPT,
            tools=[
                function_tool(
                    self.set_attention_sensitivity,
                    name="set_attention_sensitivity",
                    description=(
                        "Adjust SAA's directed-speech threshold (0..1). Higher "
                        "values mean fewer false-positives but more missed turns; "
                        "lower values mean the agent responds to weaker signals."
                    ),
                ),
                function_tool(
                    self.mute_microphone,
                    name="mute_microphone",
                    description="Pause SAA's listening (privacy mute). The agent will not hear anything until unmute_microphone is called.",
                ),
                function_tool(
                    self.unmute_microphone,
                    name="unmute_microphone",
                    description="Resume SAA's listening after a mute_microphone call.",
                ),
            ],
        )
        self._bridge = bridge
        self.passed_turns = 0

    # Pipeline override, the heart of the pre-ASR gate.
    def stt_node(
        self,
        audio: AsyncIterable[rtc.AudioFrame],
        model_settings: ModelSettings,
    ):
        return self._bridge.run_stt_node(
            audio,
            model_settings,
            default_node=Agent.default.stt_node,
            agent=self,
        )

    async def on_user_turn_completed(
        self,
        turn_ctx: Any,
        new_message: Any,
    ) -> None:
        # In pre-STT mode every transcript we see is by construction
        # SAA-blessed. We surface the latest verdict's metadata so the LLM
        # can ground its tone (e.g. "the user was looking at me with high
        # confidence" → respond definitively).
        v = self._bridge.latest_prediction()
        if v is not None and hasattr(new_message, "content"):
            tag = (
                f" (saa: cls={v.cls} conf={v.confidence:.2f} faces={v.num_faces})"
            )
            try:
                if isinstance(new_message.content, list):
                    new_message.content = [*new_message.content, tag]
                elif isinstance(new_message.content, str):
                    new_message.content = new_message.content + tag
            except Exception:  # pragma: no cover
                pass
        self.passed_turns += 1
        logger.debug("saa: turn passed (%s)", _safe_text_preview(new_message))

    # Function tools ----------------------------------------------------

    async def set_attention_sensitivity(self, threshold: float) -> str:
        """Adjust SAA's directed-speech threshold at runtime."""
        clamped = max(0.0, min(1.0, float(threshold)))
        self._bridge.set_threshold(clamped)
        return f"Attention threshold set to {clamped:.2f}."

    async def mute_microphone(self) -> str:
        self._bridge.mute()
        return "Microphone muted. I'm not listening until you ask me to unmute."

    async def unmute_microphone(self) -> str:
        self._bridge.unmute()
        return "Microphone unmuted. I'm listening again."


# ───────────────────────────────────────────────────────────────────
# Response-gate mode: SAAResponseGatedAssistant
# ───────────────────────────────────────────────────────────────────


class SAAResponseGatedAssistant(Agent):
    """Legacy response-gate assistant (STT runs on every turn).

    Use only when ``stt_node`` cannot be overridden in your pipeline or
    when the SAA SDK must own the microphone (laptop / kiosk deployment).
    """

    def __init__(self, *, gate: SAAGate) -> None:
        super().__init__(
            instructions=SYSTEM_PROMPT,
            tools=[
                function_tool(
                    self.set_attention_sensitivity,
                    name="set_attention_sensitivity",
                    description="Adjust SAA's directed-speech threshold (0..1).",
                ),
            ],
        )
        self._gate = gate
        self.passed_turns = 0
        self.suppressed_turns = 0

    async def on_user_turn_completed(
        self,
        turn_ctx: Any,
        new_message: Any,
    ) -> None:
        if self._gate.is_open():
            self.passed_turns += 1
            return
        self.suppressed_turns += 1
        logger.info(
            "saa: dropping turn (no recent device-directed verdict): %s",
            _safe_text_preview(new_message),
        )
        raise StopResponse()

    @property
    def total_turns(self) -> int:
        return self.passed_turns + self.suppressed_turns

    @property
    def suppression_rate(self) -> float:
        n = self.total_turns
        return (self.suppressed_turns / n) if n else 0.0

    async def set_attention_sensitivity(self, threshold: float) -> str:
        clamped = max(0.0, min(1.0, float(threshold)))
        self._gate.set_threshold(clamped)
        return f"Attention threshold set to {clamped:.2f}."


# ───────────────────────────────────────────────────────────────────
# Camera forwarding, multi-modal accuracy
# ───────────────────────────────────────────────────────────────────


async def _forward_video_to_saa(
    track: rtc.VideoTrack,
    bridge: SAAAudioBridge,
    *,
    fps: float = DEFAULT_VIDEO_FPS,
    jpeg_quality: int = DEFAULT_VIDEO_JPEG_QUALITY,
) -> None:
    """Pump a participant video track → JPEG → SAA at ~``fps`` Hz.

    SAA expects ~250 ms cadence at ≤ 1080p; we throttle frame extraction
    to ``fps`` to avoid wasting bandwidth on a server that ignores the
    extras. JPEG encoding happens on a background thread so the receive
    loop never blocks. Encoding falls back silently when Pillow is not
    installed, SAA audio-only scoring still works (just slightly lower
    accuracy on the directed/human-directed boundary).
    """
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        logger.warning(
            "saa: Pillow not installed, video forwarding disabled. "
            "Install with `pip install Pillow` to give SAA gaze + face features."
        )
        return

    period = 1.0 / max(0.1, fps)
    next_send = 0.0
    stream = rtc.VideoStream(track)
    try:
        async for ev in stream:
            now = asyncio.get_running_loop().time()
            if now < next_send:
                continue
            frame: rtc.VideoFrame = ev.frame
            jpeg = await asyncio.to_thread(_encode_frame_jpeg, frame, jpeg_quality, Image)
            if jpeg:
                bridge.feed_video_jpeg(jpeg)
            next_send = now + period
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover
        logger.exception("saa: video forwarding task crashed")
    finally:
        with contextlib.suppress(Exception):
            await stream.aclose()


def _encode_frame_jpeg(frame: rtc.VideoFrame, quality: int, Image) -> Optional[bytes]:  # type: ignore[no-untyped-def]
    try:
        # rtc.VideoFrame supports conversion to RGB24 for Pillow.
        rgb = frame.convert(rtc.VideoBufferType.RGB24)
        img = Image.frombytes("RGB", (rgb.width, rgb.height), bytes(rgb.data))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except Exception:  # pragma: no cover
        return None


# ───────────────────────────────────────────────────────────────────
# Worker entrypoint
# ───────────────────────────────────────────────────────────────────


async def entrypoint(ctx: JobContext) -> None:
    """LiveKit worker entrypoint.

    One ``AgentSession`` per LiveKit room. The session owns either an
    :class:`SAAAudioBridge` (pre-STT mode, default) or a :class:`SAAGate`
    (response mode). Bot-speech suppression is wired via the canonical
    1.x event ``agent_state_changed``.
    """
    _require_env("ATTENLABS_TOKEN", "OPENAI_API_KEY", "DEEPGRAM_API_KEY")

    mode = _resolve_mode()
    threshold = float(os.environ.get("SAA_THRESHOLD", "0.7"))
    gate_ttl_s = float(os.environ.get("SAA_GATE_TTL_S", "2.0"))
    enable_video = os.environ.get("SAA_ENABLE_VIDEO", "true").lower() != "false"
    video_fps = float(os.environ.get("SAA_VIDEO_FPS", str(DEFAULT_VIDEO_FPS)))
    video_quality = int(os.environ.get("SAA_VIDEO_JPEG_QUALITY", str(DEFAULT_VIDEO_JPEG_QUALITY)))

    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL if enable_video else AutoSubscribe.AUDIO_ONLY)

    if mode == "pre_stt":
        bridge = SAAAudioBridge(
            token=os.environ["ATTENLABS_TOKEN"],
            url=os.environ.get("ATTENLABS_URL"),
            threshold=threshold,
        )
        # Bind the bridge to the running event loop so SAA's WS thread
        # can hand SAA-blessed speech back to the asyncio queue.
        bridge._loop = asyncio.get_running_loop()
        bridge.start()
        assistant: Agent = SAAPreSTTAssistant(bridge=bridge)
        stat_source: Any = bridge
    else:
        gate = SAAGate(
            token=os.environ["ATTENLABS_TOKEN"],
            url=os.environ.get("ATTENLABS_URL"),
            threshold=threshold,
            gate_ttl_s=gate_ttl_s,
            enable_video=enable_video,
        )
        gate.start()
        assistant = SAAResponseGatedAssistant(gate=gate)
        stat_source = gate

    session = AgentSession(
        stt=deepgram.STT(model=os.environ.get("DEEPGRAM_MODEL", "nova-2-general")),
        llm=openai.LLM(model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini")),
        tts=openai.TTS(voice=os.environ.get("OPENAI_VOICE", "alloy")),
        vad=silero.VAD.load(),
    )

    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics(event: Any) -> None:
        try:
            usage_collector.collect(event.metrics)
        except Exception:  # pragma: no cover
            pass
        metrics.log_metrics(event.metrics)

    @session.on("agent_state_changed")
    def _on_agent_state(event: Any) -> None:
        # Drives SAA's mark_responding() on state transitions so the
        # agent doesn't trigger on its own TTS bleed. This is the
        # canonical 1.x hook for own-voice suppression.
        new_state = getattr(event, "new_state", None)
        old_state = getattr(event, "old_state", None)
        if new_state == "speaking" and old_state != "speaking":
            stat_source.mark_responding(True)
        elif old_state == "speaking" and new_state != "speaking":
            stat_source.mark_responding(False)

    video_tasks: list[asyncio.Task[None]] = []

    @ctx.room.on("track_subscribed")
    def _on_track_subscribed(
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if mode != "pre_stt" or not enable_video:
            return
        if track.kind != rtc.TrackKind.KIND_VIDEO:
            return
        if not isinstance(track, rtc.VideoTrack):
            return
        logger.info(
            "saa: forwarding video track from %s @ %.1f fps",
            participant.identity,
            video_fps,
        )
        task = asyncio.create_task(
            _forward_video_to_saa(
                track,
                stat_source,  # bridge in pre_stt mode
                fps=video_fps,
                jpeg_quality=video_quality,
            ),
            name=f"saa-video-{participant.identity}",
        )
        video_tasks.append(task)

    ctx.add_shutdown_callback(lambda: _shutdown(stat_source, assistant, video_tasks, usage_collector))

    try:
        await session.start(
            agent=assistant,
            room=ctx.room,
            room_input_options=RoomInputOptions(
                audio_enabled=True,
                video_enabled=enable_video,
                audio_sample_rate=SAA_SAMPLE_RATE if mode == "pre_stt" else 24_000,
                audio_num_channels=SAA_NUM_CHANNELS,
            ),
        )
        await session.generate_reply(
            instructions="Greet the participant in one short sentence."
        )
    except Exception:
        try:
            stat_source.stop()
        finally:
            raise


async def _shutdown(
    stat_source: Any,
    assistant: Agent,
    video_tasks: list[asyncio.Task[None]],
    usage_collector: "metrics.UsageCollector",
) -> None:
    """Per-session teardown: log savings + close SAA."""
    for t in video_tasks:
        t.cancel()
    for t in video_tasks:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await t
    try:
        summary = usage_collector.get_summary()
    except Exception:  # pragma: no cover
        summary = None
    _log_session_summary(stat_source, assistant, summary)
    try:
        stat_source.stop()
    except Exception:  # pragma: no cover
        logger.exception("saa: stop() failed")


def _log_session_summary(
    stat_source: Any,
    assistant: Agent,
    usage_summary: Any,
) -> None:
    """Emit a structured per-session summary so operators can track savings.

    Parse the key=value form into your metrics backend (Prom, Datadog,
    OpenTelemetry) or replace with a direct emit.
    """
    snap: SAAGateSnapshot = stat_source.snapshot()
    passed = getattr(assistant, "passed_turns", 0)
    suppressed = getattr(assistant, "suppressed_turns", 0)
    total = passed + suppressed
    suppression_rate = (suppressed / total) if total else 0.0

    logger.info(
        "saa-session-summary uptime_s=%.1f mode=%s "
        "turns_total=%d turns_passed=%d turns_suppressed=%d suppression_rate=%.2f "
        "speech_ready=%d preds=%d speech_seconds=%.1f saa_errors=%d "
        "upstream_audio_samples=%d upstream_video_frames=%d "
        "llm_prompt_tokens=%s llm_completion_tokens=%s tts_audio_duration=%s",
        snap.uptime_s,
        os.environ.get("SAA_GATE_MODE", "pre_stt"),
        total,
        passed,
        suppressed,
        suppression_rate,
        snap.speech_ready_count,
        snap.prediction_count,
        snap.speech_ready_total_seconds,
        snap.saa_errors,
        snap.upstream_audio_samples_fed,
        snap.upstream_video_frames_fed,
        _usage_field(usage_summary, "llm_prompt_tokens"),
        _usage_field(usage_summary, "llm_completion_tokens"),
        _usage_field(usage_summary, "tts_audio_duration"),
    )


def _usage_field(usage_summary: Any, name: str) -> str:
    if usage_summary is None:
        return "-"
    val = getattr(usage_summary, name, None)
    return str(val) if val is not None else "-"


def _prewarm(proc: JobProcess) -> None:
    """Pre-load Silero VAD so cold-start of each session is fast."""
    proc.userdata["vad"] = silero.VAD.load()


def _build_worker_options() -> WorkerOptions:
    return WorkerOptions(
        entrypoint_fnc=entrypoint,
        prewarm_fnc=_prewarm,
        agent_name=os.environ.get("AGENT_NAME", "saa-gated-assistant"),
    )


def main() -> None:
    """Module entry point, wired via ``saa-livekit-agent`` console script."""
    cli.run_app(_build_worker_options())


if __name__ == "__main__":
    main()
