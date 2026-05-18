"""SAA gate as a Pipecat ``FrameProcessor``, production-grade adapter.

This is the canonical SAA × Pipecat integration. It is **not** a thin
``feed_audio`` proxy: every surface the ``attenlabs-saa`` SDK exposes is
wired into the Pipecat pipeline so a downstream agent gets the full value
of the SAA core tech, multimodal addressee detection, gaze + face signal,
barge-in, server-side endpointing, privacy mute, agent-TTS suppression,
and observability, with one drop-in processor.

Place immediately after ``transport.input()`` and before STT::

    transport.input() ─▶ SAAGate ─▶ STT ─▶ LLM ─▶ TTS ─▶ transport.output()

What the gate does, end-to-end
──────────────────────────────

1. **Upstream-mode pre-ASR gating** (the default for every Pipecat cloud
   transport, Daily, SmallWebRTC, Twilio): the gate consumes upstream
   ``InputAudioRawFrame`` frames, resamples any-rate/any-channels PCM16
   to mono 16 kHz, chunks into 100 ms blocks (matching the server feature
   window), and pushes them through ``AttentionClient.feed_audio``. The
   SDK does not capture audio itself. SAA performs cloud-side
   classification + endpointing and re-emits each device-directed
   utterance (``cls=2``, confidence ≥ threshold) as a fresh
   ``InputAudioRawFrame`` downstream. STT credits and LLM tokens are
   spent only on directed speech.

2. **Multimodal video** (optional, opt-in): when the transport delivers
   ``InputImageRawFrame`` / ``UserImageRawFrame`` frames, the gate JPEG-
   encodes and forwards them via ``AttentionClient.feed_video`` at the
   server's 250 ms cadence. This unlocks gaze + face-presence signal in
   the prediction stream, the multimodal differentiator over wake-words.

3. **Barge-in** (optional): while the bot is speaking, a fresh
   device-directed utterance pushes an ``InterruptionFrame`` upstream so
   the LLM / TTS halts immediately instead of waiting on STT.

4. **Agent-TTS suppression**: ``BotStartedSpeakingFrame`` → ``mute()`` +
   ``mark_responding(True)``; ``BotStoppedSpeakingFrame`` reverses both.

5. **Observability sidecars**: every SAA prediction, decision, stat,
   and connection-state change is mirrored as a typed Pipecat ``DataFrame``
   (``SAAPredictionFrame``, ``SAADecisionFrame``, ``SAAStatsFrame``,
   ``SAAConnectionFrame``) so a Pipecat ``FrameObserver``, or the
   ``overlay_server.py`` SSE shim, can surface SAA's full decision log
   to ``@attenlabs/saa-overlay`` in real time.

6. **Real Pipecat metrics**: the gate generates standard Pipecat TTFB +
   processing metrics so dashboards see the SAA-induced latency without a
   custom probe. Cumulative counters live on :attr:`SAAGate.gate_metrics`.

7. **Connection-aware error propagation**: terminal SAA failures
   (``auth``, ``reconnect_failed``) push a *fatal* ``ErrorFrame``
   upstream so the pipeline shuts down cleanly instead of black-holing
   audio. Transient drops (``disconnected`` → ``reconnecting`` →
   ``reconnected``) are logged and exposed as metrics but do **not**
   crash the pipeline.

A clearly-labelled **participant-edge local-mic mode** is preserved as an
opt-in legacy path (``upstream_mode=False``) for the laptop demo where the
SAA SDK still owns the local microphone. See ``bot.py`` for both wirings.

Verified against ``pipecat-ai>=1.0`` and ``attenlabs-saa>=1.0`` (May 19,
2026 launch).
"""
from __future__ import annotations

import asyncio
import io
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import numpy as np
from loguru import logger

try:
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        CancelFrame,
        DataFrame,
        EndFrame,
        ErrorFrame,
        Frame,
        InputAudioRawFrame,
        InterruptionFrame,
        StartFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "pipecat-ai is required for the SAA Pipecat adapter. "
        "Install with: pip install 'pipecat-ai>=1.0'"
    ) from exc

try:
    from pipecat.frames.frames import InputImageRawFrame, UserImageRawFrame  # type: ignore
    _IMAGE_FRAME_TYPES: tuple[type, ...] = (InputImageRawFrame, UserImageRawFrame)
except ImportError:  # pragma: no cover
    _IMAGE_FRAME_TYPES = ()

from saa import (
    AttentionClient,
    AttentionErrorEvent,
    ConfigEvent,
    DisconnectedEvent,
    MicConfig,
    PredictionEvent,
    ReconnectConfig,
    ReconnectFailedEvent,
    ReconnectedEvent,
    ReconnectingEvent,
    SpeechReadyEvent,
    StateEvent,
    StatsEvent,
    VadEvent,
)


SAA_SAMPLE_RATE = 16_000
SAA_NUM_CHANNELS = 1
SAA_FRAME_SAMPLES = 1600
SAA_FRAME_BYTES = SAA_FRAME_SAMPLES * 2
SAA_VIDEO_INTERVAL_S = 0.25
SAA_VIDEO_MAX_BYTES = 200_000


@dataclass
class _SAAFrame(DataFrame):
    """Common base for SAA sidecar frames. Sets a stable name= for logs."""

    def __post_init__(self):
        super().__post_init__()
        self.name = f"{self.__class__.__name__}"


@dataclass
class SAAPredictionFrame(_SAAFrame):
    """Raw SAA prediction snapshot. One per ``on_prediction`` event."""

    cls: int = 0
    confidence: float = 0.0
    source: str = ""
    num_faces: int = 0
    gaze_on_device: Optional[bool] = None
    face_visible: Optional[bool] = None
    input_modalities: Optional[list[str]] = None
    threshold: float = 0.7


@dataclass
class SAADecisionFrame(_SAAFrame):
    """Overlay-shaped decision event. Matches @attenlabs/saa-overlay schema."""

    ts: str = ""
    decision: str = "idle"  # pass | drop | abstain | override | idle
    rule: str = ""
    command_preview: str = ""
    reason: str = ""


@dataclass
class SAAStatsFrame(_SAAFrame):
    """Periodic connection health snapshot (~10 s)."""

    rtt_ms: Optional[float] = None
    sent_audio: int = 0
    sent_video: int = 0
    skipped_video: int = 0
    uptime_ms: float = 0.0
    buffered_amount: int = 0
    reconnect_count: int = 0
    skipped_video_reasons: dict = field(default_factory=dict)


@dataclass
class SAAConnectionFrame(_SAAFrame):
    """WebSocket lifecycle event (connecting/connected/disconnected/etc.)."""

    state: str = "unknown"
    detail: str = ""
    code: Optional[int] = None
    attempt: Optional[int] = None
    delay_ms: Optional[float] = None


@dataclass
class SAAGateMetrics:
    """Cumulative gate counters surfaced via :attr:`SAAGate.gate_metrics`."""

    started_at_monotonic: Optional[float] = None
    upstream_audio_frames_received: int = 0
    upstream_audio_frames_dropped: int = 0
    upstream_audio_samples_fed: int = 0
    upstream_image_frames_received: int = 0
    upstream_image_frames_fed: int = 0
    upstream_image_frames_skipped_cadence: int = 0
    upstream_image_encode_failures: int = 0
    speech_ready_emitted: int = 0
    speech_ready_total_seconds: float = 0.0
    bot_speaking_suppressions: int = 0
    barge_in_emitted: int = 0
    predictions_total: int = 0
    predictions_directed: int = 0
    last_prediction_cls: Optional[int] = None
    last_prediction_confidence: Optional[float] = None
    last_prediction_source: Optional[str] = None
    last_prediction_modalities: Optional[list[str]] = None
    last_vad_probability: Optional[float] = None
    last_state: Optional[str] = None
    last_threshold: Optional[float] = None
    saa_errors: int = 0
    last_error_kind: Optional[str] = None
    last_error_title: Optional[str] = None
    disconnected_count: int = 0
    reconnect_attempts: int = 0
    reconnect_count: int = 0
    last_rtt_ms: Optional[float] = None
    sent_audio_total: int = 0
    sent_video_total: int = 0
    skipped_video_total: int = 0


def _to_mono_16k_pcm16(
    audio_bytes: bytes,
    *,
    src_rate: int,
    src_channels: int,
) -> np.ndarray:
    """Convert any-rate/any-channels PCM16 little-endian → mono 16 kHz int16."""
    if not audio_bytes:
        return np.zeros(0, dtype=np.int16)
    pcm = np.frombuffer(audio_bytes, dtype=np.int16)
    if src_channels > 1:
        n = (pcm.size // src_channels) * src_channels
        pcm = pcm[:n].reshape(-1, src_channels).astype(np.int32).mean(axis=1)
        pcm = pcm.astype(np.int16)
    if src_rate == SAA_SAMPLE_RATE:
        return pcm.astype(np.int16, copy=False)
    if pcm.size == 0:
        return pcm.astype(np.int16, copy=False)
    ratio = SAA_SAMPLE_RATE / float(src_rate)
    out_len = max(1, int(round(pcm.size * ratio)))
    src_idx = np.linspace(0.0, pcm.size - 1, num=out_len, dtype=np.float64)
    resampled = np.interp(src_idx, np.arange(pcm.size, dtype=np.float64), pcm)
    return np.clip(resampled, -32768, 32767).astype(np.int16)


def _encode_image_jpeg(
    image: bytes,
    *,
    size: Optional[tuple[int, int]] = None,
    format_hint: Optional[str] = None,
    quality: int = 70,
) -> Optional[bytes]:
    """Encode an upstream image frame to JPEG for ``feed_video``."""
    if not image:
        return None
    if format_hint and format_hint.upper() in ("JPEG", "JPG"):
        return bytes(image)
    if size is None:
        return bytes(image) if (image[:2] == b"\xff\xd8") else None
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    try:
        mode = (format_hint or "RGB").upper()
        if mode not in ("RGB", "RGBA", "L"):
            mode = "RGB"
        img = Image.frombytes(mode, size, bytes(image))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SAAGate(FrameProcessor):
    """Pipecat ``FrameProcessor`` that gates audio with SAA."""

    _decision_listeners: list[Callable[["SAADecisionFrame"], Any]]

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        url: Optional[str] = None,
        threshold: float = 0.7,
        upstream_mode: bool = True,
        forward_upstream_video: bool = True,
        enable_barge_in: bool = True,
        suppress_during_bot_speech: bool = True,
        emit_user_speaking_frames: bool = True,
        emit_sidecar_frames: bool = True,
        passthrough_upstream_audio: bool = False,
        passthrough_during_warmup: bool = False,
        reconnect: Optional[ReconnectConfig] = None,
        mic_config: Optional[MicConfig] = None,
        enable_video: bool = True,
        name: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(name=name or "SAAGate", **kwargs)

        resolved_token = token or os.environ.get("ATTENLABS_TOKEN")
        if not resolved_token:
            raise ValueError(
                "SAAGate requires an ATTENLABS_TOKEN. Pass token=... or set the "
                "environment variable. Get a token at attentionlabs.ai/dashboard."
            )

        self._upstream_mode = bool(upstream_mode)
        self._forward_upstream_video = bool(forward_upstream_video)
        self._enable_barge_in = bool(enable_barge_in)
        self._suppress_during_bot_speech = bool(suppress_during_bot_speech)
        self._emit_user_speaking_frames = bool(emit_user_speaking_frames)
        self._emit_sidecar_frames = bool(emit_sidecar_frames)
        self._passthrough_upstream_audio = bool(passthrough_upstream_audio)
        self._passthrough_during_warmup = bool(passthrough_during_warmup)

        client_kwargs: dict[str, Any] = {
            "token": resolved_token,
            "url": url,
            "initial_threshold": threshold,
        }
        if reconnect is not None:
            client_kwargs["reconnect"] = reconnect

        if self._upstream_mode:
            self._client = AttentionClient(upstream_mode=True, **client_kwargs)
        else:
            self._client = AttentionClient(
                audio=mic_config or MicConfig(),
                enable_audio=True,
                enable_video=enable_video,
                **client_kwargs,
            )

        self._upstream_buffer = bytearray()
        self._last_video_send_monotonic: float = 0.0

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_lock = threading.Lock()
        self._started = False
        self._warmed_up = False
        self._bot_speaking = False
        self._fatal_pushed = False

        self._gate_metrics = SAAGateMetrics(last_threshold=threshold)
        self._decision_listeners = []

        self._wire_saa_listeners()

    @property
    def gate_metrics(self) -> SAAGateMetrics:
        return self._gate_metrics

    @property
    def attention_client(self) -> AttentionClient:
        return self._client

    @property
    def upstream_mode(self) -> bool:
        return self._upstream_mode

    @property
    def is_warm(self) -> bool:
        return self._warmed_up

    def can_generate_metrics(self) -> bool:
        return True

    def set_threshold(self, value: float) -> None:
        self._client.set_threshold(value)
        self._gate_metrics.last_threshold = float(value)

    def mark_responding(self, responding: bool) -> None:
        self._mark_responding(bool(responding))

    def get_sdk_stats(self) -> StatsEvent:
        return self._client.get_stats()

    def add_decision_listener(
        self, listener: Callable[["SAADecisionFrame"], Awaitable[None] | None]
    ) -> None:
        self._decision_listeners.append(listener)

    def _wire_saa_listeners(self) -> None:
        @self._client.on_speech_ready
        def _on_speech(event: SpeechReadyEvent) -> None:
            self._dispatch(self._handle_speech_ready(event))

        @self._client.on_prediction
        def _on_prediction(event: PredictionEvent) -> None:
            self._dispatch(self._handle_prediction(event))

        @self._client.on_vad
        def _on_vad(event: VadEvent) -> None:
            self._gate_metrics.last_vad_probability = float(event.probability)

        @self._client.on_state
        def _on_state(event: StateEvent) -> None:
            self._gate_metrics.last_state = event.state

        @self._client.on_config
        def _on_config(event: ConfigEvent) -> None:
            self._gate_metrics.last_threshold = float(event.model_class2_threshold)

        @self._client.on_stats
        def _on_stats(event: StatsEvent) -> None:
            self._dispatch(self._handle_stats(event))

        @self._client.on_warmup_complete
        def _on_warm() -> None:
            self._warmed_up = True
            logger.info("saa: warmup complete, gate now closed-loop")
            self._dispatch(self._handle_connection(state="warm"))

        @self._client.on_connecting
        def _on_connecting() -> None:
            self._dispatch(self._handle_connection(state="connecting"))

        @self._client.on_connected
        def _on_connected() -> None:
            self._dispatch(self._handle_connection(state="connected"))

        @self._client.on_disconnected
        def _on_disconnected(event: DisconnectedEvent) -> None:
            self._gate_metrics.disconnected_count += 1
            self._dispatch(
                self._handle_connection(
                    state="disconnected", detail=event.reason, code=event.code,
                )
            )

        @self._client.on_reconnecting
        def _on_reconnecting(event: ReconnectingEvent) -> None:
            self._gate_metrics.reconnect_attempts += 1
            self._dispatch(
                self._handle_connection(
                    state="reconnecting", detail=event.cause_reason,
                    code=event.cause_code, attempt=event.attempt,
                    delay_ms=event.delay_ms,
                )
            )

        @self._client.on_reconnected
        def _on_reconnected(event: ReconnectedEvent) -> None:
            self._gate_metrics.reconnect_count = event.reconnect_count
            self._dispatch(
                self._handle_connection(state="reconnected", attempt=event.attempt)
            )

        @self._client.on_reconnect_failed
        def _on_reconnect_failed(event: ReconnectFailedEvent) -> None:
            self._dispatch(
                self._handle_terminal(
                    state="reconnect_failed", detail=event.last_cause_reason,
                    code=event.last_cause_code, attempt=event.attempts,
                )
            )

        @self._client.on_error
        def _on_error(event: AttentionErrorEvent) -> None:
            self._gate_metrics.saa_errors += 1
            self._gate_metrics.last_error_kind = event.kind
            self._gate_metrics.last_error_title = event.title
            logger.error(
                "saa[{}]: {}, {}{}",
                event.kind, event.title, event.message,
                f" ({event.detail})" if event.detail else "",
            )
            if event.kind == "auth":
                self._dispatch(self._handle_terminal(state="auth_failed", detail=event.message))
            else:
                self._dispatch(self._handle_connection(state=f"error:{event.kind}", detail=event.message))

    def _dispatch(self, coro) -> None:
        with self._loop_lock:
            loop = self._loop
        if loop is None or not loop.is_running():
            coro.close()
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()

    async def _handle_speech_ready(self, event: SpeechReadyEvent) -> None:
        if not self._started or event.audio_pcm16.size == 0:
            return

        await self.stop_ttfb_metrics()

        self._gate_metrics.speech_ready_emitted += 1
        self._gate_metrics.speech_ready_total_seconds += event.duration_sec
        logger.info(
            "saa: device-directed utterance {:.2f}s ({} samples) → STT",
            event.duration_sec, event.audio_pcm16.size,
        )

        if self._enable_barge_in and self._bot_speaking:
            self._gate_metrics.barge_in_emitted += 1
            logger.info("saa: barge-in, pushing InterruptionFrame upstream")
            await self.push_frame(InterruptionFrame(), FrameDirection.UPSTREAM)

        if self._emit_user_speaking_frames:
            await self.push_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)

        await self.push_frame(
            InputAudioRawFrame(
                audio=event.audio_pcm16.tobytes(),
                sample_rate=SAA_SAMPLE_RATE,
                num_channels=SAA_NUM_CHANNELS,
            ),
            FrameDirection.DOWNSTREAM,
        )

        if self._emit_user_speaking_frames:
            await self.push_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

        await self._emit_decision(
            decision="pass",
            reason=f"{event.duration_sec:.2f}s utterance",
            command_preview=f"{event.audio_pcm16.size} samples → STT",
        )

    async def _handle_prediction(self, event: PredictionEvent) -> None:
        self._gate_metrics.predictions_total += 1
        if event.cls == 2:
            self._gate_metrics.predictions_directed += 1
        self._gate_metrics.last_prediction_cls = event.cls
        self._gate_metrics.last_prediction_confidence = event.confidence
        self._gate_metrics.last_prediction_source = str(event.source) if event.source else None
        self._gate_metrics.last_prediction_modalities = (
            list(event.input_modalities) if event.input_modalities else None
        )

        threshold = float(self._gate_metrics.last_threshold or self._client.threshold)
        if event.cls == 2 and event.confidence >= threshold:
            decision = "pass"
        elif event.cls == 2:
            decision = "abstain"
        else:
            decision = "drop"
        reason_bits = [f"cls={event.cls}", f"conf={event.confidence:.2f}"]
        if event.gaze_on_device is not None:
            reason_bits.append(f"gaze={'on' if event.gaze_on_device else 'off'}")
        if event.face_visible is not None:
            reason_bits.append(f"face={'y' if event.face_visible else 'n'}")
        if event.input_modalities:
            reason_bits.append("+".join(event.input_modalities))

        if self._emit_sidecar_frames:
            await self.push_frame(
                SAAPredictionFrame(
                    cls=event.cls,
                    confidence=float(event.confidence),
                    source=str(event.source) if event.source else "",
                    num_faces=int(event.num_faces),
                    gaze_on_device=event.gaze_on_device,
                    face_visible=event.face_visible,
                    input_modalities=(
                        list(event.input_modalities) if event.input_modalities else None
                    ),
                    threshold=threshold,
                ),
                FrameDirection.DOWNSTREAM,
            )
        if decision != "pass":
            await self._emit_decision(
                decision=decision,
                rule=f"saa.cls{event.cls}",
                reason=" ".join(reason_bits),
            )

    async def _handle_stats(self, event: StatsEvent) -> None:
        self._gate_metrics.last_rtt_ms = event.rtt_ms
        self._gate_metrics.sent_audio_total = int(event.sent_audio)
        self._gate_metrics.sent_video_total = int(event.sent_video)
        self._gate_metrics.skipped_video_total = int(event.skipped_video)
        self._gate_metrics.reconnect_count = int(event.reconnect_count)
        if self._emit_sidecar_frames:
            await self.push_frame(
                SAAStatsFrame(
                    rtt_ms=event.rtt_ms,
                    sent_audio=event.sent_audio,
                    sent_video=event.sent_video,
                    skipped_video=event.skipped_video,
                    uptime_ms=event.uptime_ms,
                    buffered_amount=event.buffered_amount,
                    reconnect_count=event.reconnect_count,
                    skipped_video_reasons=dict(event.skipped_video_reasons),
                ),
                FrameDirection.DOWNSTREAM,
            )

    async def _handle_connection(
        self, *, state: str, detail: str = "",
        code: Optional[int] = None, attempt: Optional[int] = None,
        delay_ms: Optional[float] = None,
    ) -> None:
        if self._emit_sidecar_frames:
            await self.push_frame(
                SAAConnectionFrame(
                    state=state, detail=detail, code=code,
                    attempt=attempt, delay_ms=delay_ms,
                ),
                FrameDirection.DOWNSTREAM,
            )

    async def _handle_terminal(
        self, *, state: str, detail: str = "",
        code: Optional[int] = None, attempt: Optional[int] = None,
    ) -> None:
        await self._handle_connection(state=state, detail=detail, code=code, attempt=attempt)
        if self._fatal_pushed:
            return
        self._fatal_pushed = True
        msg = f"SAA terminal failure: {state}, {detail or 'no detail'}"
        try:
            await self.push_error(msg, fatal=True)
        except TypeError:  # pragma: no cover
            await self.push_frame(ErrorFrame(error=msg), FrameDirection.UPSTREAM)

    async def _emit_decision(
        self, *, decision: str, rule: str = "",
        command_preview: str = "", reason: str = "",
    ) -> None:
        if not self._emit_sidecar_frames and not self._decision_listeners:
            return
        frame = SAADecisionFrame(
            ts=_now_iso(),
            decision=decision,
            rule=rule,
            command_preview=command_preview,
            reason=reason,
        )
        if self._emit_sidecar_frames:
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)
        for listener in self._decision_listeners:
            try:
                result = listener(frame)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                logger.exception("saa: decision listener raised")

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self._on_start(frame)
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (EndFrame, CancelFrame)):
            await self._on_stop(frame)
            await self.push_frame(frame, direction)
            return

        if (
            self._suppress_during_bot_speech
            and isinstance(frame, BotStartedSpeakingFrame)
        ):
            self._bot_speaking = True
            await asyncio.to_thread(self._mark_responding, True)
            self._gate_metrics.bot_speaking_suppressions += 1
            await self.push_frame(frame, direction)
            return

        if (
            self._suppress_during_bot_speech
            and isinstance(frame, BotStoppedSpeakingFrame)
        ):
            self._bot_speaking = False
            await asyncio.to_thread(self._mark_responding, False)
            await self.push_frame(frame, direction)
            return

        if (
            isinstance(frame, InputAudioRawFrame)
            and direction == FrameDirection.DOWNSTREAM
        ):
            await self._handle_upstream_audio(frame)
            return

        if (
            _IMAGE_FRAME_TYPES
            and isinstance(frame, _IMAGE_FRAME_TYPES)
            and direction == FrameDirection.DOWNSTREAM
        ):
            await self._handle_upstream_image(frame)
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    async def _handle_upstream_audio(self, frame: InputAudioRawFrame) -> None:
        self._gate_metrics.upstream_audio_frames_received += 1
        await self.start_ttfb_metrics()
        await self.start_processing_metrics()

        fed_any = False
        if self._upstream_mode and self._started:
            mono16k = _to_mono_16k_pcm16(
                frame.audio,
                src_rate=int(frame.sample_rate or SAA_SAMPLE_RATE),
                src_channels=int(frame.num_channels or 1),
            )
            if mono16k.size:
                self._upstream_buffer.extend(mono16k.tobytes())
                while len(self._upstream_buffer) >= SAA_FRAME_BYTES:
                    chunk = bytes(self._upstream_buffer[:SAA_FRAME_BYTES])
                    del self._upstream_buffer[:SAA_FRAME_BYTES]
                    self._gate_metrics.upstream_audio_samples_fed += SAA_FRAME_SAMPLES
                    await asyncio.to_thread(self._client.feed_audio, chunk)
                    fed_any = True

        passthrough = self._passthrough_upstream_audio or (
            self._passthrough_during_warmup and not self._warmed_up
        )
        await self.stop_processing_metrics()

        if passthrough:
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)
        else:
            self._gate_metrics.upstream_audio_frames_dropped += 1
        if not fed_any and not passthrough:
            await self.stop_ttfb_metrics()

    async def _handle_upstream_image(self, frame: Frame) -> None:
        self._gate_metrics.upstream_image_frames_received += 1
        if not (self._upstream_mode and self._started and self._forward_upstream_video):
            return
        now = time.monotonic()
        if now - self._last_video_send_monotonic < SAA_VIDEO_INTERVAL_S:
            self._gate_metrics.upstream_image_frames_skipped_cadence += 1
            return
        image_bytes: bytes = getattr(frame, "image", b"") or b""
        size = getattr(frame, "size", None)
        fmt = getattr(frame, "format", None)
        jpeg = await asyncio.to_thread(
            _encode_image_jpeg, bytes(image_bytes), size=size, format_hint=fmt,
        )
        if not jpeg or len(jpeg) > SAA_VIDEO_MAX_BYTES:
            self._gate_metrics.upstream_image_encode_failures += 1
            return
        self._last_video_send_monotonic = now
        self._gate_metrics.upstream_image_frames_fed += 1
        await asyncio.to_thread(self._client.feed_video, jpeg)

    async def _on_start(self, frame: StartFrame) -> None:
        if self._started:
            return
        with self._loop_lock:
            self._loop = (
                self.get_event_loop()
                if hasattr(self, "get_event_loop")
                else asyncio.get_running_loop()
            )
        loop_started_at = self._loop.time() if self._loop else None
        self._gate_metrics.started_at_monotonic = loop_started_at
        self._upstream_buffer.clear()
        self._last_video_send_monotonic = 0.0
        self._fatal_pushed = False

        try:
            await asyncio.to_thread(self._client.start)
        except Exception:
            logger.exception("SAA AttentionClient failed to start")
            try:
                await self.push_error(
                    "SAA AttentionClient failed to start, check ATTENLABS_TOKEN "
                    "and network connectivity.",
                    fatal=True,
                )
            except TypeError:  # pragma: no cover
                await self.push_frame(
                    ErrorFrame(error="SAA AttentionClient failed to start"),
                    FrameDirection.UPSTREAM,
                )
            raise
        self._started = True
        logger.info(
            "saa: gate active (mode={}, threshold={:.2f}, audio_in={}, video={})",
            "upstream" if self._upstream_mode else "local-mic",
            self._client.threshold,
            frame.audio_in_sample_rate,
            self._forward_upstream_video if self._upstream_mode else self._client.enable_video,
        )

    async def _on_stop(self, frame: Frame) -> None:
        if not self._started:
            return
        try:
            await asyncio.to_thread(self._client.stop)
        except Exception:
            logger.exception("SAA AttentionClient failed to stop cleanly")
        finally:
            self._started = False
            self._warmed_up = False
            self._bot_speaking = False
            self._upstream_buffer.clear()
            with self._loop_lock:
                self._loop = None
        logger.info(
            "saa: gate stopped (emitted={} fed_samples={} dropped={} barge_in={} reconnects={})",
            self._gate_metrics.speech_ready_emitted,
            self._gate_metrics.upstream_audio_samples_fed,
            self._gate_metrics.upstream_audio_frames_dropped,
            self._gate_metrics.barge_in_emitted,
            self._gate_metrics.reconnect_count,
        )

    def _mark_responding(self, responding: bool) -> None:
        try:
            if responding:
                self._client.mark_responding(True)
                self._client.mute()
            else:
                self._client.unmute()
                self._client.mark_responding(False)
        except Exception:  # noqa: BLE001
            logger.exception("saa: mark_responding(%s) failed", responding)
