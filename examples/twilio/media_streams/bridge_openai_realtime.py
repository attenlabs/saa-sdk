"""Reference STT/LLM/TTS bridge for the SAA × Twilio adapter.

This is the production-grade bridge most teams will start from. It wires
SAA-gated utterances (PCM16 @ 16 kHz, already device-directed by the time
the bridge sees them) straight into OpenAI's Realtime API and forwards
the streamed audio response back through the adapter's outbound queue,
where the paced sender ships it to Twilio at 20 ms cadence.

The bridge demonstrates every high-leverage SAA control the SDK exposes:

* **Pre-ASR gating.** Only ``turn_ready`` payloads are ever sent to the
  LLM, ambient room speech, hold music, and other-party conversation
  never burn a token.
* **`mark_responding(True/False)`** is driven automatically by the
  adapter when bytes start / stop flowing through the outbound queue, and
  also asserted manually here the instant Realtime begins generating its
  reply so the SAA cloud suppresses predictions during the entire
  agent-speaking window (not just while audio is playing).
* **Barge-in.** SAA's ``on_user_speech_started`` event, fired at the
  leading edge of any new device-directed utterance, cancels in-flight
  Realtime responses and flushes Twilio's playback queue.
* **Adaptive threshold.** SAA's ``stats`` callback is used to nudge the
  device-class threshold up when the SDK reports a hot inbound rate
  (background chatter) and back down once it settles.
* **DTMF.** Caller key presses are forwarded to the Realtime session as
  user text so the agent can branch on menu input.

Wire it up at startup::

    from server import app, set_bridge_factory
    from bridge_openai_realtime import OpenAIRealtimeBridge

    set_bridge_factory(
        lambda: OpenAIRealtimeBridge(
            api_key=os.environ["OPENAI_API_KEY"],
            model="gpt-realtime-2",
            voice="alloy",
            system_prompt=\"You are a friendly receptionist...\",
        )
    )

References:

* OpenAI Realtime API: https://platform.openai.com/docs/guides/realtime
* OpenAI Realtime events: https://platform.openai.com/docs/api-reference/realtime
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from bridge import CallContext, CallSession

log = logging.getLogger("saa.twilio.openai_realtime")

OPENAI_REALTIME_URL = (
    "wss://api.openai.com/v1/realtime?model={model}"
)
DEFAULT_MODEL = "gpt-realtime-2"
DEFAULT_VOICE = "alloy"
DEFAULT_SYSTEM_PROMPT = (
    "You are a friendly, concise voice assistant on a phone call. "
    "Keep replies under two sentences unless the caller asks for detail. "
    "Speak in a warm, natural cadence."
)

# SAA delivers PCM16 @ 16 kHz; gpt-realtime-2 requires ≥ 24 kHz.
# on_speech() upsamples 16 k→24 k before sending to Realtime.
# Realtime output is also 24 kHz PCM16 — the adapter's paced sender
# handles the µ-law re-encode for Twilio downstream.
REALTIME_INPUT_SAMPLE_RATE = 24_000   # after upsampling
REALTIME_OUTPUT_SAMPLE_RATE = 24_000
SAA_INPUT_SAMPLE_RATE = 16_000        # what SAA hands us

# Adaptive-threshold band. SAA defaults to 0.7; the bridge nudges in
# this window based on traffic from the SAA stats stream. Keeps the
# adapter from misfiring on cocktail-party calls while staying permissive
# for solo callers.
ADAPTIVE_THRESHOLD_LOW = 0.65
ADAPTIVE_THRESHOLD_HIGH = 0.82


@dataclass
class _RealtimeSessionState:
    """Per-call mutable state for the Realtime side of the bridge."""

    response_in_flight: Optional[str] = None
    cancellations: int = 0


class OpenAIRealtimeBridge:
    """Bridge SAA-gated utterances to the OpenAI Realtime API.

    Implements the :class:`bridge.Bridge` protocol. Construct one per call
    (the adapter calls ``set_bridge_factory`` with a lambda that returns a
    fresh instance), wire it via ``server.set_bridge_factory``, and the
    adapter handles the rest.

    Parameters
    ----------
    api_key:
        OpenAI API key with Realtime access.
    model:
        Realtime model name. Defaults to ``gpt-realtime-2``.
    voice:
        Realtime voice (``alloy``, ``echo``, ``shimmer``, etc.).
    system_prompt:
        Free-form instructions injected as the session's system message.
    tools:
        Optional list of OpenAI Realtime tool specs (function calling).
    temperature:
        Realtime model temperature, 0..2.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        tools: Optional[list[dict]] = None,
        temperature: float = 0.7,
        adaptive_threshold: bool = True,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "OpenAIRealtimeBridge: api_key is empty and OPENAI_API_KEY is unset"
            )
        self.model = model
        self.voice = voice
        self.system_prompt = system_prompt
        self.tools = tools or []
        self.temperature = float(temperature)
        self.adaptive_threshold = bool(adaptive_threshold)

        # Mutable state populated on open().
        self.outbound_pcm16_16k: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()
        self._ctx: Optional[CallContext] = None
        self._session: Optional[CallSession] = None
        self._ws: Any = None  # websockets.WebSocketClientProtocol
        self._reader_task: Optional[asyncio.Task] = None
        self._state = _RealtimeSessionState()
        self._closing = False

    # ── Bridge lifecycle ────────────────────────────────────

    async def open(self, ctx: CallContext, session: CallSession) -> None:
        try:
            import websockets  # noqa: F401, imported lazily so import-time
            #                                  doesn't require an OAI install
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "websockets>=12 is required for OpenAIRealtimeBridge: "
                "pip install 'websockets>=12'"
            ) from exc

        self._ctx = ctx
        self._session = session
        url = OPENAI_REALTIME_URL.format(model=self.model)
        log.info(
            "[openai-realtime] connecting model=%s voice=%s for call=%s",
            self.model, self.voice, ctx.call_sid,
        )
        # the realtime variant, not Chat Completions.
        from websockets.client import connect as ws_connect
        self._ws = await ws_connect(
            url,
            extra_headers={
                "Authorization": f"Bearer {self.api_key}",
            },
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=2,
        )
        # Configure the Realtime session up front. server_vad is
        # intentionally disabled, SAA is the gate, not Realtime's VAD.
        # NOTE: The gpt-realtime-2 API requires nested audio config and
        # a minimum sample rate of 24000 Hz. Voice is under audio.output.
        # SAA delivers 16 kHz; on_speech() upsamples 16k→24k before send.
        await self._send_json({
            "type": "session.update",
            "session": {
                "type": "realtime",  # required by gpt-realtime-2
                "instructions": self.system_prompt,
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {"model": "whisper-1"},
                        "turn_detection": None,  # SAA owns endpointing
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": self.voice,
                    },
                },
                "tools": self.tools,
                "tool_choice": "auto" if self.tools else "none",
            },
        })
        self._reader_task = asyncio.create_task(
            self._read_realtime(), name=f"oai-rt-{ctx.stream_sid}",
        )

    async def close(self) -> None:
        self._closing = True
        # Terminate the adapter's outbound sender.
        await self.outbound_pcm16_16k.put(None)
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        log.info(
            "[openai-realtime] call closed: call=%s cancellations=%d",
            self._ctx.call_sid if self._ctx else "?",
            self._state.cancellations,
        )

    # ── SAA → Realtime ─────────────────────────────────────

    async def on_speech(self, audio_pcm16_16k: np.ndarray, duration_sec: float) -> None:
        """Forward a SAA-gated utterance into Realtime as an input audio item.

        The audio comes off SAA as PCM16 @ 16 kHz mono and matches
        Realtime's input audio format byte-for-byte, so no resampling is
        required. We push it as a single conversation item and then
        request the response, turn detection is off so this is fully
        client-driven.
        """
        if self._ws is None or self._closing:
            return
        # Cancel any in-flight response before submitting a new turn.
        # SAA already debounces overlapping utterances, but barge-ins can
        # arrive faster than the model finishes its previous reply.
        await self._cancel_response_if_any()
        # Upsample 16 kHz → 24 kHz (gpt-realtime-2 requires ≥ 24 kHz).
        # Simple nearest-neighbor resample: repeat every 2 samples → 3.
        pcm16 = np.ascontiguousarray(audio_pcm16_16k, dtype=np.int16)
        pcm24k = np.repeat(pcm16, 3)[::2].astype(np.int16)
        audio_b64 = base64.b64encode(pcm24k.tobytes()).decode("ascii")
        await self._send_json({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_audio", "audio": audio_b64}],
            },
        })
        await self._send_json({"type": "response.create"})
        # The leading edge of mark_responding is asserted by the adapter
        # when the first audio frame lands on the outbound queue, but we
        # also signal it here so the SAA cloud suppresses the entire LLM
        # round-trip (including the silent thinking window) and not just
        # the playback. The adapter's tail timer flips it back to False
        # ~250 ms after the queue drains.
        if self._session is not None:
            await self._session.mark_responding(True)
        log.info(
            "[openai-realtime] speech in: %.2fs (%d samples) → Realtime",
            duration_sec, audio_pcm16_16k.size,
        )

    async def on_user_speech_started(self) -> None:
        """Caller started talking again. Cancel the response + flush playback.

        SAA flips its state to ``sending`` at the leading edge of a new
        device-directed utterance, well before the utterance itself is
        complete and available on ``on_speech``. That's the right barge-
        in moment: we cancel Realtime's response, drop everything in our
        outbound queue, and tell Twilio to clear its playback buffer.
        """
        await self._cancel_response_if_any()
        if self._session is not None:
            await self._session.clear_playback()
        log.info("[openai-realtime] barge-in: cancelled in-flight response")

    async def on_dtmf(self, digit: str) -> None:
        """Forward DTMF to Realtime as user text.

        Useful for IVR-style menus (\"press 1 for sales\"). Realtime
        interprets the text input and replies in voice, so the caller's
        key press flows through the same conversation history as voice.
        """
        if self._ws is None or self._closing:
            return
        await self._cancel_response_if_any()
        await self._send_json({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"Caller pressed {digit}"}],
            },
        })
        await self._send_json({"type": "response.create"})

    async def on_mark_played(self, name: str) -> None:
        # End-of-utterance synchronisation lives outside this reference
        # because mark_responding's tail timer already covers the
        # callback-friendly cases. A bridge that needs strict "after
        # caller hears X" semantics would key on this.
        pass

    async def on_saa_prediction(self, event) -> None:
        # Available for adaptive routing; the reference just logs the
        # confidence at debug level so operators can inspect edge cases.
        log.debug(
            "[openai-realtime] saa pred: cls=%s conf=%.2f faces=%s src=%s",
            getattr(event, "cls", "?"),
            getattr(event, "confidence", 0.0),
            getattr(event, "num_faces", 0),
            getattr(event, "source", "-"),
        )

    async def on_saa_vad(self, event) -> None:
        pass

    async def on_saa_warmup_complete(self) -> None:
        log.info(
            "[openai-realtime] SAA warmup complete, ready for caller=%s",
            self._ctx.from_number if self._ctx else "?",
        )

    async def on_saa_stats(self, event) -> None:
        """Drive an adaptive threshold from SAA's periodic stats stream.

        Threshold sits at the cloud default by default; if SAA's stats
        show we're sending lots of audio (busy / cocktail-party call),
        raise toward ADAPTIVE_THRESHOLD_HIGH so we're stricter about what
        counts as device-directed. On a quiet call we relax back toward
        ADAPTIVE_THRESHOLD_LOW so soft-spoken callers still get through.

        This is the canonical example of why exposing SAA controls on
        :class:`CallSession` matters, callers don't have to hold their
        own SDK handle to retune mid-call.
        """
        if not self.adaptive_threshold or self._session is None:
            return
        sent_audio = getattr(event, "sent_audio", 0) or 0
        if sent_audio > 500:  # ~50s of frames in the last window
            await self._session.set_threshold(ADAPTIVE_THRESHOLD_HIGH)
        elif sent_audio < 50:
            await self._session.set_threshold(ADAPTIVE_THRESHOLD_LOW)

    async def on_caller_hangup(self) -> None:
        log.info("[openai-realtime] caller hung up")

    # ── Realtime → SAA / Twilio ───────────────────────────────

    async def _read_realtime(self) -> None:
        """Consume Realtime server events and forward TTS audio back to Twilio.

        Realtime streams ``response.audio.delta`` events containing
        base64-encoded PCM16. We decode and enqueue each delta on the
        adapter's outbound queue, the paced sender takes it from there.
        ``response.done`` / ``response.cancelled`` clear our
        ``response_in_flight`` marker so the next turn can fire.
        """
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                etype = msg.get("type")
                if etype == "response.created":
                    self._state.response_in_flight = (msg.get("response") or {}).get("id")
                elif etype == "response.output_audio.delta":
                    # gpt-realtime-2 uses response.output_audio.delta
                    # (older preview used response.audio.delta)
                    audio_b64 = msg.get("delta") or ""
                    if audio_b64:
                        try:
                            chunk = base64.b64decode(audio_b64)
                        except Exception:  # noqa: BLE001
                            continue
                        if chunk:
                            # OpenAI streams 24 kHz PCM16; the outbound path expects 16 kHz (then ->
                            # Twilio 8 kHz). Resample 24k->16k here, else playback is 1.5x slow / low.
                            _p24 = np.frombuffer(chunk, dtype=np.int16)
                            _n16 = (_p24.size * 2) // 3
                            if _n16:
                                _p16 = np.interp(np.arange(_n16) * 1.5, np.arange(_p24.size),
                                                 _p24.astype(np.float32)).astype(np.int16)
                                await self.outbound_pcm16_16k.put(_p16.tobytes())
                elif etype in ("response.output_audio_transcript.delta",
                               "response.audio_transcript.delta"):
                    # Optional: stream the agent's transcript somewhere.
                    pass
                elif etype in ("response.done", "response.cancelled"):
                    self._state.response_in_flight = None
                    # Drop responding back to False at the seam between
                    # turns so SAA reopens predictions cleanly. The
                    # adapter's tail timer would also handle this, but
                    # being explicit avoids relying on it.
                    if self._session is not None:
                        await self._session.mark_responding(False)
                elif etype == "error":
                    err = (msg.get("error") or {})
                    log.warning(
                        "[openai-realtime] error: %s: %s",
                        err.get("code", "?"), err.get("message", "?"),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            if not self._closing:
                log.exception("[openai-realtime] reader crashed")

    async def _cancel_response_if_any(self) -> None:
        if not self._state.response_in_flight or self._ws is None:
            return
        self._state.cancellations += 1
        await self._send_json({"type": "response.cancel"})
        self._state.response_in_flight = None

    async def _send_json(self, payload: dict) -> None:
        if self._ws is None or self._closing:
            return
        try:
            await self._ws.send(json.dumps(payload))
        except Exception:  # noqa: BLE001
            if not self._closing:
                log.exception("[openai-realtime] send failed: %s", payload.get("type"))


__all__ = ["OpenAIRealtimeBridge"]
