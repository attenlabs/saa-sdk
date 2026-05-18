"""SAA × LiveKit Agents, production adapter.

This module exposes three integration surfaces, each saving a different
layer of voice-pipeline cost:

1. :class:`SAAAudioBridge`, **pre-ASR gate (recommended)**.
   Owns an ``AttentionClient`` in ``upstream_mode=True``: the SDK never
   captures a local mic. Instead, every participant audio frame from
   LiveKit's room is resampled to mono 16 kHz PCM16, chunked into 100 ms
   blocks, and forwarded to SAA via :meth:`AttentionClient.feed_audio`.
   SAA endpoints + classifies; the bridge re-emits **only**
   ``speech_ready`` audio as a fresh ``rtc.AudioFrame`` stream for the
   default ``Agent.stt_node`` to transcribe.

   This is the path that actually saves STT cost: the inner STT plugin
   is never invoked on non-directed speech. Wire it via
   :meth:`Agent.stt_node`::

       class SAAPreSTTAssistant(Agent):
           async def stt_node(self, audio, model_settings):
               async for ev in self._bridge.run_stt_node(
                   audio, model_settings, default_node=Agent.default.stt_node
               ):
                   yield ev

   The bridge also accepts JPEG video frames via
   :meth:`SAAAudioBridge.feed_video_jpeg`. Subscribe to the participant
   camera track and forward JPEGs to make SAA multi-modal, the model
   uses gaze/face presence to raise its directed-speech accuracy.

2. :class:`SAAGate`, **response gate (legacy / lightweight)**.
   Wraps an ``AttentionClient`` in default mode (SDK owns the mic);
   exposes a single ``is_open()`` oracle the agent checks in
   ``on_user_turn_completed``. STT still runs on every turn, only
   LLM + TTS are skipped for non-directed turns. Use this when the
   worker is on-device (laptop, kiosk) and you want the SDK's own
   mic capture path.

3. :class:`SAAGatedSTT`, drop-in ``stt.STT`` wrapper that blanks
   final transcripts when the gate is closed. The inner STT still
   runs, so this only saves LLM + TTS. Provided for pipelines that
   can't override ``Agent.stt_node`` directly.

Verified against ``attenlabs-saa`` 1.0.0 and ``livekit-agents`` 1.0.x.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

# LiveKit is an optional runtime dependency. We import lazily so that
# `python test_smoke_shape.py` and `import saa_gate` succeed in environments
# that have only `attenlabs-saa` installed (e.g. CI shape-check, doc builds).
try:
    from livekit import rtc
    from livekit.agents import stt, utils  # noqa: F401
    from livekit.agents.stt import (
        STT,
        SpeechData,
        SpeechEvent,
        SpeechEventType,
        SpeechStream,
        STTCapabilities,
    )
    _LIVEKIT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _LIVEKIT_AVAILABLE = False

    # Minimal type stubs so the module still parses + the smoke test passes
    # without livekit-agents on the path. These are *only* used as base classes
    # for class statements; they're never instantiated when LiveKit is absent.
    class _Stub:
        def __init__(self, *args, **kwargs): ...

    class STT(_Stub):  # type: ignore[no-redef]
        ...

    class SpeechStream(_Stub):  # type: ignore[no-redef]
        ...

    class STTCapabilities:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs): ...

    class SpeechEvent:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs): ...

    class SpeechEventType:  # type: ignore[no-redef]
        FINAL_TRANSCRIPT = "final_transcript"
        INTERIM_TRANSCRIPT = "interim_transcript"

    class SpeechData:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs): ...

    class _RtcStub:
        class AudioFrame:
            def __init__(self, *args, **kwargs): ...

        class AudioTrack:
            ...

    rtc = _RtcStub()  # type: ignore[assignment]


from saa import AttentionClient  # noqa: E402

logger = logging.getLogger("saa.livekit")

DEFAULT_THRESHOLD = 0.7
DEFAULT_GATE_TTL_S = 2.0
SAA_SAMPLE_RATE = 16_000
SAA_NUM_CHANNELS = 1
# Server feature window, 100 ms @ 16 kHz mono int16 = 1600 samples = 3200 bytes.
SAA_FRAME_SAMPLES = 1600
SAA_FRAME_BYTES = SAA_FRAME_SAMPLES * 2
# Re-emitted frames are chunked at 20 ms so STT plugins (Deepgram, AssemblyAI)
# see input cadence comparable to a raw LiveKit track.
SPEECH_OUT_FRAME_MS = 20
SPEECH_OUT_FRAME_SAMPLES = (SAA_SAMPLE_RATE * SPEECH_OUT_FRAME_MS) // 1000


def _to_mono_16k_pcm16(
    audio_bytes: bytes,
    *,
    src_rate: int,
    src_channels: int,
) -> np.ndarray:
    """Convert any-rate / any-channels PCM16 little-endian → mono 16 kHz int16.

    Linear-interpolation resampling is more than adequate for SAA classification
    and the STT plugins downstream (the model is robust to mild aliasing).
    """
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


# ───────────────────────────────────────────────────────────────────
# Shared verdict bookkeeping
# ───────────────────────────────────────────────────────────────────


@dataclass
class _Verdict:
    """The most recent SAA evidence that the live audio is for the device."""

    cls: int
    confidence: float
    is_speech_ready: bool
    at: float
    num_faces: int = 0
    source: str = ""

    def expired(self, ttl_s: float) -> bool:
        return (time.monotonic() - self.at) > ttl_s


@dataclass
class SAAGateSnapshot:
    """Lightweight per-session counters for operators / dashboards.

    Emit at session close to track suppression rate over time. None of these
    leave the process by default, wire them to your metrics backend (Prom,
    Datadog, OpenTelemetry) explicitly.
    """

    speech_ready_count: int
    prediction_count: int
    last_verdict_at: Optional[float]
    latest_cls: Optional[int]
    latest_confidence: Optional[float]
    uptime_s: float
    upstream_audio_samples_fed: int = 0
    upstream_video_frames_fed: int = 0
    speech_ready_total_seconds: float = 0.0
    saa_errors: int = 0


# ───────────────────────────────────────────────────────────────────
# SAAGate, legacy response-gate orchestrator (local-mic mode)
# ───────────────────────────────────────────────────────────────────


class SAAGate:
    """Response-gate orchestrator (legacy local-mic deployment).

    Owns an ``AttentionClient`` in default mode, the SDK captures audio
    (and optionally video) on whatever machine the worker is running on.
    Exposes a single :meth:`is_open` oracle that an ``Agent`` subclass
    checks in ``on_user_turn_completed``::

        gate = SAAGate(threshold=0.72)

        class Assistant(Agent):
            async def on_user_turn_completed(self, turn_ctx, new_message):
                if not gate.is_open():
                    raise StopResponse()

        gate.start()
        try:
            await session.start(agent=Assistant(), room=room)
        finally:
            gate.stop()

    The gate is open iff SAA emitted ``speech_ready`` within the last
    ``gate_ttl_s`` seconds. ``speech_ready`` fires only for utterances the
    server classified as device-directed at or above ``threshold``.

    **Cost saved**: LLM tokens + TTS minutes per suppressed turn, plus the
    wrong-reply UX cost. STT still runs on every turn. For pre-ASR savings
    (STT cost too), use :class:`SAAAudioBridge` and override
    :meth:`Agent.stt_node` instead.
    """

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        url: Optional[str] = None,
        threshold: float = DEFAULT_THRESHOLD,
        gate_ttl_s: float = DEFAULT_GATE_TTL_S,
        client: Optional[AttentionClient] = None,
        enable_video: bool = True,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            self._client = AttentionClient(
                token=token or os.environ.get("ATTENLABS_TOKEN"),
                url=url,
                initial_threshold=threshold,
                enable_video=enable_video,
            )

        self._gate_ttl_s = float(gate_ttl_s)
        self._lock = threading.Lock()
        self._verdict: Optional[_Verdict] = None
        self._started = False
        self._started_at: Optional[float] = None
        self._speech_ready_count = 0
        self._prediction_count = 0
        self._saa_errors = 0

        self._client.on_speech_ready(self._on_speech_ready)
        self._client.on_prediction(self._on_prediction)
        if hasattr(self._client, "on_error"):
            self._client.on_error(self._on_error)

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Open the underlying ``AttentionClient``."""
        self._client.start()
        self._started = True
        self._started_at = time.monotonic()

    def stop(self) -> None:
        """Close the underlying ``AttentionClient``. Idempotent."""
        if not self._started:
            return
        self._client.stop()
        self._started = False

    # ── gate state ─────────────────────────────────────────────────

    def is_open(self, *, ttl_s: Optional[float] = None) -> bool:
        """Return True iff a recent SAA verdict permits the current turn."""
        ttl = ttl_s if ttl_s is not None else self._gate_ttl_s
        with self._lock:
            v = self._verdict
        if v is None:
            return False
        if v.expired(ttl):
            return False
        return v.is_speech_ready

    def latest_prediction(self) -> Optional[_Verdict]:
        with self._lock:
            return self._verdict

    def reset(self) -> None:
        with self._lock:
            self._verdict = None

    def snapshot(self) -> SAAGateSnapshot:
        with self._lock:
            v = self._verdict
            speech_ready = self._speech_ready_count
            preds = self._prediction_count
            errors = self._saa_errors
        uptime = (
            time.monotonic() - self._started_at if self._started_at is not None else 0.0
        )
        return SAAGateSnapshot(
            speech_ready_count=speech_ready,
            prediction_count=preds,
            last_verdict_at=v.at if v else None,
            latest_cls=v.cls if v else None,
            latest_confidence=v.confidence if v else None,
            uptime_s=uptime,
            saa_errors=errors,
        )

    # ── passthroughs to AttentionClient ────────────────────────────

    def mute(self) -> None:
        """Pause upstream audio (privacy mute)."""
        self._client.mute()

    def unmute(self) -> None:
        """Resume upstream audio after a mute."""
        self._client.unmute()

    def mark_responding(self, responding: bool) -> None:
        """Tell SAA the agent is speaking, predictions are paused."""
        self._client.mark_responding(responding)

    def set_threshold(self, value: float) -> None:
        """Update the device-class confidence threshold (0..1)."""
        self._client.set_threshold(value)

    # ── internal handlers ──────────────────────────────────────────

    def _on_speech_ready(self, event) -> None:
        with self._lock:
            self._verdict = _Verdict(
                cls=2,
                confidence=1.0,
                is_speech_ready=True,
                at=time.monotonic(),
            )
            self._speech_ready_count += 1
        logger.debug("saa: speech_ready (%.2fs), gate OPEN", event.duration_sec)

    def _on_prediction(self, event) -> None:
        with self._lock:
            self._prediction_count += 1
            existing = self._verdict
            if existing is None or existing.is_speech_ready is False:
                self._verdict = _Verdict(
                    cls=int(event.cls),
                    confidence=float(event.confidence),
                    is_speech_ready=False,
                    at=time.monotonic(),
                    num_faces=int(getattr(event, "num_faces", 0) or 0),
                    source=str(getattr(event, "source", "") or ""),
                )

    def _on_error(self, event) -> None:
        with self._lock:
            self._saa_errors += 1
        logger.error(
            "saa: %s, %s%s",
            getattr(event, "title", "error"),
            getattr(event, "message", ""),
            f" ({event.detail})" if getattr(event, "detail", None) else "",
        )


# ───────────────────────────────────────────────────────────────────
# SAAAudioBridge, pre-ASR gate, multi-modal, signal-only
# ───────────────────────────────────────────────────────────────────


@dataclass
class _SpeechFragment:
    pcm16: np.ndarray
    duration_sec: float


class SAAAudioBridge:
    """Pre-ASR gate adapter, wraps an ``AttentionClient`` in upstream mode.

    The bridge does **not** open a local microphone. Instead it accepts:

    * ``rtc.AudioFrame`` from the LiveKit room (via :meth:`feed_audio_frame`),
      resamples to mono 16 kHz, chunks into 100 ms blocks, and forwards to
      SAA via :meth:`AttentionClient.feed_audio`.
    * Optional JPEG-encoded camera frames (via :meth:`feed_video_jpeg`)
      forwarded to :meth:`AttentionClient.feed_video` so SAA can use
      gaze + face presence in its directed-speech verdict (multi-modal
      accuracy is the main reason to wire the participant's camera track).

    It exposes the SAA-blessed audio as an async iterator of
    ``rtc.AudioFrame`` (:meth:`iter_speech_frames`), and a turnkey
    :meth:`run_stt_node` helper that you call from your
    ``Agent.stt_node`` override::

        class Assistant(Agent):
            def __init__(self, bridge):
                super().__init__(instructions=PROMPT)
                self._bridge = bridge

            async def stt_node(self, audio, model_settings):
                async for ev in self._bridge.run_stt_node(
                    audio,
                    model_settings,
                    default_node=Agent.default.stt_node,
                    agent=self,
                ):
                    yield ev

    Cost saved: STT credits + LLM tokens + TTS minutes for every
    non-directed utterance (background media, side conversation, agent's own TTS bleed).
    """

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        url: Optional[str] = None,
        threshold: float = DEFAULT_THRESHOLD,
        client: Optional[AttentionClient] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            self._client = AttentionClient(
                token=token or os.environ.get("ATTENLABS_TOKEN"),
                url=url,
                initial_threshold=threshold,
                upstream_mode=True,  # SDK never opens a local mic
            )

        self._loop = loop
        self._lock = threading.Lock()
        self._verdict: Optional[_Verdict] = None
        self._started = False
        self._started_at: Optional[float] = None
        self._speech_ready_count = 0
        self._prediction_count = 0
        self._saa_errors = 0
        self._upstream_audio_samples_fed = 0
        self._upstream_video_frames_fed = 0
        self._speech_ready_total_seconds = 0.0
        self._upstream_buffer = bytearray()
        self._speech_queue: "asyncio.Queue[_SpeechFragment]" = asyncio.Queue()

        self._client.on_speech_ready(self._on_speech_ready)
        self._client.on_prediction(self._on_prediction)
        if hasattr(self._client, "on_error"):
            self._client.on_error(self._on_error)

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
        self._client.start()
        self._started = True
        self._started_at = time.monotonic()

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._client.stop()
        finally:
            self._started = False
            self._upstream_buffer.clear()
            # Drain any pending speech fragments so a fresh session starts clean.
            try:
                while not self._speech_queue.empty():
                    self._speech_queue.get_nowait()
            except Exception:
                pass

    # ── feed: audio + video ────────────────────────────────────────

    def feed_audio_frame(self, frame: "rtc.AudioFrame") -> None:
        """Resample, chunk, and forward a LiveKit ``rtc.AudioFrame`` to SAA."""
        if not self._started:
            return
        data = bytes(frame.data)
        src_rate = int(getattr(frame, "sample_rate", SAA_SAMPLE_RATE))
        src_channels = int(getattr(frame, "num_channels", 1))
        mono16k = _to_mono_16k_pcm16(data, src_rate=src_rate, src_channels=src_channels)
        if mono16k.size == 0:
            return
        self._upstream_buffer.extend(mono16k.tobytes())
        while len(self._upstream_buffer) >= SAA_FRAME_BYTES:
            chunk = bytes(self._upstream_buffer[:SAA_FRAME_BYTES])
            del self._upstream_buffer[:SAA_FRAME_BYTES]
            with self._lock:
                self._upstream_audio_samples_fed += SAA_FRAME_SAMPLES
            self._client.feed_audio(chunk)

    def feed_video_jpeg(self, jpeg: bytes) -> None:
        """Forward a JPEG-encoded camera frame to SAA for multi-modal scoring."""
        if not self._started or not jpeg:
            return
        try:
            self._client.feed_video(jpeg)
        except Exception:  # pragma: no cover
            logger.exception("saa: feed_video failed")
            return
        with self._lock:
            self._upstream_video_frames_fed += 1

    # ── consume: SAA-blessed audio for STT ─────────────────────────

    async def iter_speech_frames(self) -> AsyncIterator["rtc.AudioFrame"]:
        """Yield ``rtc.AudioFrame`` objects for every SAA-blessed utterance.

        Each ``speech_ready`` payload is sliced into 20 ms frames so
        downstream STT plugins see a cadence comparable to a raw LiveKit
        participant track.
        """
        while True:
            fragment = await self._speech_queue.get()
            pcm = fragment.pcm16
            for start in range(0, pcm.size, SPEECH_OUT_FRAME_SAMPLES):
                chunk = pcm[start : start + SPEECH_OUT_FRAME_SAMPLES]
                if chunk.size == 0:
                    continue
                yield rtc.AudioFrame(
                    data=chunk.tobytes(),
                    sample_rate=SAA_SAMPLE_RATE,
                    num_channels=SAA_NUM_CHANNELS,
                    samples_per_channel=int(chunk.size),
                )

    async def run_stt_node(
        self,
        audio: AsyncIterable["rtc.AudioFrame"],
        model_settings: Any,
        *,
        default_node: Callable[..., AsyncIterable[Any]],
        agent: Any,
    ) -> AsyncIterator[Any]:
        """Drive an STT node from SAA-gated audio.

        Spawns a drain task that pulls every upstream frame from ``audio``
        and forwards it to SAA (so SAA's endpointer sees the full stream).
        Yields ``SpeechEvent``s produced by ``default_node`` running over
        :meth:`iter_speech_frames`, i.e. STT only sees the SAA-blessed
        utterances.
        """

        async def _drain() -> None:
            try:
                async for frame in audio:
                    self.feed_audio_frame(frame)
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover
                logger.exception("saa: drain task failed")

        drain_task = asyncio.create_task(_drain(), name="saa-audio-drain")
        try:
            inner = default_node(agent, self.iter_speech_frames(), model_settings)
            if asyncio.iscoroutine(inner):
                inner = await inner
            if inner is None:
                return
            async for ev in inner:
                yield ev
        finally:
            drain_task.cancel()
            try:
                await drain_task
            except (asyncio.CancelledError, Exception):
                pass

    # ── verdict + snapshot ─────────────────────────────────────────

    def latest_prediction(self) -> Optional[_Verdict]:
        with self._lock:
            return self._verdict

    def snapshot(self) -> SAAGateSnapshot:
        with self._lock:
            v = self._verdict
            uptime = (
                time.monotonic() - self._started_at
                if self._started_at is not None
                else 0.0
            )
            return SAAGateSnapshot(
                speech_ready_count=self._speech_ready_count,
                prediction_count=self._prediction_count,
                last_verdict_at=v.at if v else None,
                latest_cls=v.cls if v else None,
                latest_confidence=v.confidence if v else None,
                uptime_s=uptime,
                upstream_audio_samples_fed=self._upstream_audio_samples_fed,
                upstream_video_frames_fed=self._upstream_video_frames_fed,
                speech_ready_total_seconds=self._speech_ready_total_seconds,
                saa_errors=self._saa_errors,
            )

    # ── passthroughs ───────────────────────────────────────────────

    def mute(self) -> None:
        self._client.mute()

    def unmute(self) -> None:
        self._client.unmute()

    def mark_responding(self, responding: bool) -> None:
        self._client.mark_responding(responding)

    def set_threshold(self, value: float) -> None:
        self._client.set_threshold(value)

    # ── internal SAA event handlers ────────────────────────────────

    def _on_speech_ready(self, event) -> None:
        pcm = getattr(event, "audio_pcm16", None)
        duration = float(getattr(event, "duration_sec", 0.0) or 0.0)
        if pcm is None or pcm.size == 0:
            return

        with self._lock:
            self._verdict = _Verdict(
                cls=2,
                confidence=1.0,
                is_speech_ready=True,
                at=time.monotonic(),
            )
            self._speech_ready_count += 1
            self._speech_ready_total_seconds += duration

        # Enqueue the SAA-blessed PCM for downstream STT consumption.
        fragment = _SpeechFragment(pcm16=np.asarray(pcm, dtype=np.int16), duration_sec=duration)
        loop = self._loop
        if loop is None or not loop.is_running():
            # Best-effort: drop the fragment rather than block on the WS thread.
            logger.warning("saa: speech_ready before bridge bound to a loop; dropping %.2fs", duration)
            return
        try:
            asyncio.run_coroutine_threadsafe(self._speech_queue.put(fragment), loop)
        except RuntimeError:  # pragma: no cover
            logger.warning("saa: failed to enqueue speech_ready (loop closed)")

    def _on_prediction(self, event) -> None:
        with self._lock:
            self._prediction_count += 1
            existing = self._verdict
            if existing is None or existing.is_speech_ready is False:
                self._verdict = _Verdict(
                    cls=int(event.cls),
                    confidence=float(event.confidence),
                    is_speech_ready=False,
                    at=time.monotonic(),
                    num_faces=int(getattr(event, "num_faces", 0) or 0),
                    source=str(getattr(event, "source", "") or ""),
                )

    def _on_error(self, event) -> None:
        with self._lock:
            self._saa_errors += 1
        logger.error(
            "saa: %s, %s%s",
            getattr(event, "title", "error"),
            getattr(event, "message", ""),
            f" ({event.detail})" if getattr(event, "detail", None) else "",
        )


# ───────────────────────────────────────────────────────────────────
# SAAGatedSTT, drop-in stt.STT wrapper (legacy)
# ───────────────────────────────────────────────────────────────────


class SAAGatedSTT(STT):
    """Drop-in ``stt.STT`` that blanks transcripts when SAA says no.

    Wraps an inner STT plugin (e.g. ``deepgram.STT()``) and forwards audio
    through. When the inner STT emits ``FINAL_TRANSCRIPT``, the gate is
    consulted, if closed, the transcript is replaced with an empty string,
    so the LLM sees a no-op turn.

    Honest cost trade-off: the inner STT still runs (you still pay STT
    cost). Only LLM + TTS are saved per suppressed turn. Prefer
    :class:`SAAAudioBridge` + ``Agent.stt_node`` override for true pre-ASR
    savings.
    """

    def __init__(self, *, inner: STT, gate: SAAGate, ttl_s: Optional[float] = None) -> None:
        if not _LIVEKIT_AVAILABLE:
            raise SystemExit(
                "livekit-agents required for SAAGatedSTT: pip install livekit-agents"
            )
        super().__init__(capabilities=getattr(inner, "capabilities", STTCapabilities()))
        self._inner = inner
        self._gate = gate
        self._ttl_s = ttl_s

    async def _recognize_impl(self, *args, **kwargs):  # type: ignore[override]
        if not self._gate.is_open(ttl_s=self._ttl_s):
            return SpeechEvent(
                type=SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[SpeechData(language="", text="")],
            )
        return await self._inner._recognize_impl(*args, **kwargs)

    def stream(self, *args, **kwargs) -> SpeechStream:  # type: ignore[override]
        return _SAAGatedSpeechStream(
            inner=self._inner.stream(*args, **kwargs),
            gate=self._gate,
            ttl_s=self._ttl_s,
        )


class _SAAGatedSpeechStream(SpeechStream):
    """Wraps an inner SpeechStream; suppresses finals when the gate is closed."""

    def __init__(self, *, inner: SpeechStream, gate: SAAGate, ttl_s: Optional[float]) -> None:
        super().__init__()
        self._inner = inner
        self._gate = gate
        self._ttl_s = ttl_s

    def push_frame(self, frame) -> None:  # type: ignore[override]
        self._inner.push_frame(frame)

    def end_input(self) -> None:  # type: ignore[override]
        self._inner.end_input()

    async def aclose(self) -> None:  # type: ignore[override]
        await self._inner.aclose()

    def __aiter__(self):  # type: ignore[override]
        return self

    async def __anext__(self):  # type: ignore[override]
        ev = await self._inner.__anext__()
        if getattr(ev, "type", None) == SpeechEventType.FINAL_TRANSCRIPT:
            if not self._gate.is_open(ttl_s=self._ttl_s):
                logger.debug("saa: final transcript suppressed by gate")
                return SpeechEvent(
                    type=SpeechEventType.FINAL_TRANSCRIPT,
                    alternatives=[SpeechData(language="", text="")],
                )
        return ev


# ───────────────────────────────────────────────────────────────────
# SAAAudioStream, async iterator of SAA-gated PCM16 frames (legacy)
# ───────────────────────────────────────────────────────────────────


class SAAAudioStream:
    """Yields LiveKit ``rtc.AudioFrame``s for SAA-blessed utterances.

    Use this when you want SAA to drive endpointing and feed your own STT
    or LLM pipeline directly from PCM16 frames::

        stream = SAAAudioStream(token=os.environ['ATTENLABS_TOKEN'])
        stream.start()
        async for frame in stream:
            await my_stt.push_frame(frame)

    The yielded frames are mono PCM16 @ 16 kHz, exactly what most STTs and
    OpenAI Realtime expect. The SDK captures from the local mic, so this
    is the participant-edge deployment helper. For server-side / room-track
    deployments, prefer :class:`SAAAudioBridge` (upstream mode).
    """

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        url: Optional[str] = None,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self._client = AttentionClient(
            token=token or os.environ.get("ATTENLABS_TOKEN"),
            url=url,
            initial_threshold=threshold,
        )
        self._queue: "asyncio.Queue[rtc.AudioFrame]" = asyncio.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._started = False

        @self._client.on_speech_ready
        def _on_speech(event):
            loop = self._loop
            if loop is None:
                return
            frame = rtc.AudioFrame(
                data=event.audio_pcm16.tobytes(),
                sample_rate=SAA_SAMPLE_RATE,
                num_channels=SAA_NUM_CHANNELS,
                samples_per_channel=event.audio_pcm16.size,
            )
            asyncio.run_coroutine_threadsafe(self._queue.put(frame), loop)

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._client.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._client.stop()
        self._started = False

    def mark_responding(self, responding: bool) -> None:
        self._client.mark_responding(responding)

    def __aiter__(self) -> AsyncIterator["rtc.AudioFrame"]:
        return self

    async def __anext__(self) -> "rtc.AudioFrame":
        if not self._started:
            raise StopAsyncIteration
        return await self._queue.get()

    async def subscribe(self, _track: "rtc.AudioTrack") -> AsyncIterator["rtc.AudioFrame"]:
        if not self._started:
            self.start()
        try:
            while True:
                yield await self._queue.get()
        finally:
            self.stop()


__all__ = [
    "SAAGate",
    "SAAGateSnapshot",
    "SAAGatedSTT",
    "SAAAudioStream",
    "SAAAudioBridge",
    "DEFAULT_THRESHOLD",
    "DEFAULT_GATE_TTL_S",
    "SAA_SAMPLE_RATE",
    "SAA_NUM_CHANNELS",
    "SAA_FRAME_SAMPLES",
    "SPEECH_OUT_FRAME_MS",
]
