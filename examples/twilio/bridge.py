"""Bridge + CallSession interfaces for SAA-gated Twilio voice agents.

The adapter (``server.py``) handles Twilio Media Streams and SAA gating;
a :class:`Bridge` is the seam between it and your STT / LLM / TTS stack.

Lifecycle, per call:

    session = TwilioCallSession(...)        # adapter creates one per call
    await bridge.open(ctx, session)         # adapter hands the session in

    # 0+ on_speech calls as the caller talks (gated by SAA)
    # 0+ on_dtmf calls if the caller presses keys
    # 0+ on_user_speech_started calls (barge-in signal, caller talking again)
    # 0+ on_mark_played calls when Twilio confirms a <mark> reached the caller

    await bridge.close()                    # exactly once, even on disconnect

Outbound TTS audio reaches the caller via two interchangeable channels:

* push raw bytes to ``bridge.outbound_pcm16_16k`` (back-pressure friendly
  ``asyncio.Queue``, the adapter paces and chunks for you), or
* call ``session.send_audio(pcm16_16k)`` directly when you need finer
  control (mid-stream ``clear`` for barge-in, named ``mark`` for playback
  synchronisation, programmatic hangup).

This module ships :class:`LoggingBridge` as a reference; the README
points to ``bridge_openai_realtime.py`` for a full STT/LLM/TTS bridge.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Protocol, Union

import numpy as np

log = logging.getLogger("saa.twilio.bridge")


@dataclass
class CallContext:
    """Per-call metadata Twilio surfaces in the ``start`` event."""

    call_sid: str
    stream_sid: str
    account_sid: str
    from_number: str = ""
    to_number: str = ""
    direction: str = "inbound"  # "inbound" | "outbound"
    custom_parameters: Optional[dict] = None


class CallSession(Protocol):
    """Per-call control surface the adapter hands to your :class:`Bridge`.

    Methods are awaited on the FastAPI event loop and are safe to call
    from any task that has the session reference. All methods are no-ops
    once the call has ended.

    SAA controls (``mark_responding`` / ``mute`` / ``unmute`` /
    ``set_threshold``) are the high-leverage telephony hooks documented in
    the SDK README:

      * ``mark_responding(True)`` while the agent's TTS is playing tells the
        SAA server to suppress predictions during playback, so SAA does NOT
        fire ``speech_ready`` on its own TTS bleed coming back through the
        carrier echo path. Pair with ``mark_responding(False)`` when the
        agent finishes speaking.
      * ``mute`` / ``unmute`` are the privacy controls, PCM is dropped at
        the client and never reaches the SAA server while muted.
      * ``set_threshold`` retunes the device-class confidence threshold
        mid-call (raise in noisy environments; lower for quiet callers).

    The default :class:`LoggingBridge` and the reference
    ``bridge_openai_realtime.OpenAIRealtimeBridge`` both wire
    ``mark_responding`` automatically around their TTS playback windows.
    """

    call_sid: str
    stream_sid: str

    async def send_audio(self, pcm16_16k: Union[bytes, bytearray, np.ndarray]) -> None: ...
    async def clear_playback(self) -> None: ...
    async def send_mark(self, name: str) -> None: ...
    async def hangup(self) -> None: ...
    # SAA control surface (forwarded onto the per-call AttentionClient).
    async def mark_responding(self, responding: bool) -> None: ...
    async def mute(self) -> None: ...
    async def unmute(self) -> None: ...
    async def set_threshold(self, value: float) -> None: ...
    @property
    def is_open(self) -> bool: ...


class Bridge(Protocol):
    """What the adapter expects from your STT/LLM/TTS implementation.

    ``open`` is called exactly once with the per-call context and a
    :class:`CallSession`. After that the adapter dispatches SAA-gated
    speech, DTMF, and barge-in events to the matching ``on_*`` methods.
    ``close`` is called exactly once when the call ends, regardless of
    which side hung up.

    Callbacks run on the FastAPI event loop. SAA's own ``speech_ready``
    fires on the SDK's receive thread; the adapter dispatches it across
    the loop boundary before ``on_speech`` is awaited, so listeners may
    use any ``asyncio``-compatible API.
    """

    outbound_pcm16_16k: "asyncio.Queue[Optional[bytes]]"

    async def open(self, ctx: CallContext, session: CallSession) -> None: ...
    async def on_speech(self, audio_pcm16_16k: np.ndarray, duration_sec: float) -> None: ...
    async def on_user_speech_started(self) -> None: ...
    async def on_dtmf(self, digit: str) -> None: ...
    async def on_mark_played(self, name: str) -> None: ...
    # Optional SAA telemetry pass-through. Default implementations on
    # ``LoggingBridge`` are no-ops; richer bridges can use the prediction
    # confidence + source to adapt threshold, switch personas, etc.
    async def on_saa_prediction(self, event: "object") -> None: ...
    async def on_saa_vad(self, event: "object") -> None: ...
    async def on_saa_warmup_complete(self) -> None: ...
    async def on_saa_stats(self, event: "object") -> None: ...
    async def on_caller_hangup(self) -> None: ...
    async def close(self) -> None: ...


class LoggingBridge:
    """Reference Bridge that logs every event and produces no speech.

    Useful for verifying the adapter end-to-end (place a real call to your
    Twilio number, watch ``[bridge]`` lines stream out as SAA gates the
    caller's audio). Replace with a real bridge in production, see
    ``bridge_openai_realtime.py`` for a full STT/LLM/TTS reference.
    """

    def __init__(self) -> None:
        # Sentinel ``None`` on the outbound queue terminates the sender.
        self.outbound_pcm16_16k: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()
        self._ctx: Optional[CallContext] = None
        self._session: Optional[CallSession] = None

    async def open(self, ctx: CallContext, session: CallSession) -> None:
        self._ctx = ctx
        self._session = session
        log.info(
            "[bridge] call open: call_sid=%s stream_sid=%s from=%s to=%s direction=%s",
            ctx.call_sid, ctx.stream_sid, ctx.from_number, ctx.to_number, ctx.direction,
        )

    async def on_speech(self, audio_pcm16_16k: np.ndarray, duration_sec: float) -> None:
        log.info(
            "[bridge] speechReady: %.2fs (%d samples PCM16 @ 16 kHz), forward to STT/LLM here",
            duration_sec, audio_pcm16_16k.size,
        )
        # >>> Wire your STT/LLM/TTS stack here. <<<
        # Typical shape:
        #     transcript = await stt.transcribe(audio_pcm16_16k)
        #     reply_audio = await llm_then_tts(transcript)  # PCM16 @ 16 kHz
        #     await self.outbound_pcm16_16k.put(reply_audio.tobytes())

    async def on_user_speech_started(self) -> None:
        # SAA noticed the caller started talking again, flush any in-flight
        # TTS so we don't speak over them. Real bridges should also cancel
        # their LLM/TTS generation here.
        if self._session is not None:
            await self._session.clear_playback()
        log.info("[bridge] barge-in: caller started talking again")

    async def on_dtmf(self, digit: str) -> None:
        log.info("[bridge] dtmf: %s", digit)

    async def on_mark_played(self, name: str) -> None:
        log.debug("[bridge] mark played: %s", name)

    async def on_saa_prediction(self, event: object) -> None:
        log.debug("[bridge] saa prediction: %r", event)

    async def on_saa_vad(self, event: object) -> None:
        log.debug("[bridge] saa vad: %r", event)

    async def on_saa_warmup_complete(self) -> None:
        log.info("[bridge] saa warmup complete, server ready")

    async def on_saa_stats(self, event: object) -> None:
        log.debug("[bridge] saa stats: %r", event)

    async def on_caller_hangup(self) -> None:
        log.info("[bridge] caller hung up")

    async def close(self) -> None:
        await self.outbound_pcm16_16k.put(None)  # terminate sender
        log.info("[bridge] call closed")


__all__ = ["Bridge", "CallContext", "CallSession", "LoggingBridge"]
