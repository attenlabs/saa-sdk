"""Composable STT + LLM + TTS bridge: Deepgram → OpenAI Chat → ElevenLabs.

When the OpenAI Realtime model isn't the right fit (different STT/TTS
vendor mandates, model selection, on-prem latency, language coverage),
drop in this bridge instead. It demonstrates the canonical three-stage
pipeline against SAA's gated output:

    SAA turn_ready (PCM16 16 kHz)
        → Deepgram WebSocket STT
             → OpenAI Chat Completions (streaming)
                  → ElevenLabs streaming TTS (PCM16 16 kHz)
                       → adapter outbound queue → Twilio

Every SDK call uses the official Python clients (``deepgram-sdk``,
``openai``, ``elevenlabs``). All three are optional installs, the bridge
raises a clear ImportError if the user wires it up without the deps.

Key SAA features exercised:

* ``mark_responding(True/False)`` framing the LLM+TTS window
* ``on_user_speech_started`` barge-in cancels the current LLM stream and
  flushes Twilio's playback queue (via ``session.clear_playback``)
* ``set_threshold`` driven by SAA prediction confidence statistics
* DTMF tunnelled into the LLM context as a synthetic user message

Usage::

    set_bridge_factory(
        lambda: DeepgramOpenAIElevenLabsBridge(
            deepgram_api_key=os.environ["DEEPGRAM_API_KEY"],
            openai_api_key=os.environ["OPENAI_API_KEY"],
            elevenlabs_api_key=os.environ["ELEVENLABS_API_KEY"],
            voice_id=\"21m00Tcm4TlvDq8ikWAM\",  # Rachel
            model=\"gpt-4o\",
        )
    )
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

import numpy as np

from bridge import CallContext, CallSession

log = logging.getLogger("saa.twilio.dg_oai_el")

DEFAULT_OAI_MODEL = "gpt-4o"
DEFAULT_DG_MODEL = "nova-2-phonecall"
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs "Rachel"
DEFAULT_SYSTEM_PROMPT = (
    "You are a friendly, concise voice assistant on a phone call. "
    "Reply in one or two sentences. Use natural cadence."
)


class DeepgramOpenAIElevenLabsBridge:
    """Polyglot STT/LLM/TTS bridge.

    Each utterance is transcribed by Deepgram, fed as a user turn into a
    streaming OpenAI Chat Completions call, and the LLM's text response is
    piped chunk-by-chunk into an ElevenLabs streaming TTS connection.
    Audio chunks flow onto the adapter's outbound queue as soon as
    ElevenLabs produces them; the paced sender ships them to Twilio.
    """

    def __init__(
        self,
        *,
        deepgram_api_key: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        elevenlabs_api_key: Optional[str] = None,
        voice_id: str = DEFAULT_VOICE_ID,
        model: str = DEFAULT_OAI_MODEL,
        deepgram_model: str = DEFAULT_DG_MODEL,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        temperature: float = 0.6,
        max_history_turns: int = 12,
    ) -> None:
        self.dg_key = deepgram_api_key or os.environ.get("DEEPGRAM_API_KEY", "")
        self.oai_key = openai_api_key or os.environ.get("OPENAI_API_KEY", "")
        self.el_key = elevenlabs_api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        for name, value in (
            ("DEEPGRAM_API_KEY", self.dg_key),
            ("OPENAI_API_KEY", self.oai_key),
            ("ELEVENLABS_API_KEY", self.el_key),
        ):
            if not value:
                raise ValueError(
                    f"DeepgramOpenAIElevenLabsBridge: {name} is unset "
                    f"(pass via kwarg or environment)"
                )
        self.voice_id = voice_id
        self.model = model
        self.deepgram_model = deepgram_model
        self.system_prompt = system_prompt
        self.temperature = float(temperature)
        self.max_history_turns = int(max_history_turns)

        self.outbound_pcm16_16k: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()
        self._ctx: Optional[CallContext] = None
        self._session: Optional[CallSession] = None
        self._history: list[dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
        ]
        self._current_turn: Optional[asyncio.Task] = None
        self._closing = False
        self._oai: Any = None
        self._dg: Any = None

    async def open(self, ctx: CallContext, session: CallSession) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "openai>=1.0 is required for DeepgramOpenAIElevenLabsBridge: "
                "pip install 'openai>=1.0'"
            ) from exc
        try:
            from deepgram import DeepgramClient  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "deepgram-sdk>=3 is required: pip install 'deepgram-sdk>=3'"
            ) from exc
        try:
            from elevenlabs.client import AsyncElevenLabs  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "elevenlabs>=1.0 is required: pip install 'elevenlabs>=1.0'"
            ) from exc

        self._ctx = ctx
        self._session = session
        self._oai = AsyncOpenAI(api_key=self.oai_key)
        # Deepgram pre-recorded REST is enough at SAA-segmented utterance
        # lengths (typically 1–6s). Streaming WebSocket STT would let us
        # start the LLM mid-utterance, but SAA's whole value proposition
        # is that we ALREADY know the utterance is complete and
        # device-directed by the time the bridge sees it.
        from deepgram import DeepgramClient
        self._dg = DeepgramClient(self.dg_key)
        log.info(
            "[dg-oai-el] bridge ready: call=%s dg=%s oai=%s voice=%s",
            ctx.call_sid, self.deepgram_model, self.model, self.voice_id,
        )

    async def close(self) -> None:
        self._closing = True
        await self._cancel_current_turn()
        await self.outbound_pcm16_16k.put(None)
        log.info("[dg-oai-el] bridge closed")

    # ── SAA -> pipeline ─────────────────────────────────────────

    async def on_speech(self, audio_pcm16_16k: np.ndarray, duration_sec: float) -> None:
        if self._closing or self._oai is None or self._dg is None:
            return
        await self._cancel_current_turn()
        self._current_turn = asyncio.create_task(
            self._run_turn(audio_pcm16_16k, duration_sec),
            name=f"dg-oai-el-turn-{self._ctx.stream_sid if self._ctx else '?'}",
        )

    async def on_user_speech_started(self) -> None:
        await self._cancel_current_turn()
        if self._session is not None:
            await self._session.clear_playback()
        log.info("[dg-oai-el] barge-in: cancelled in-flight turn")

    async def on_dtmf(self, digit: str) -> None:
        await self._cancel_current_turn()
        self._history.append({"role": "user", "content": f"[Caller pressed {digit}]"})
        self._trim_history()
        self._current_turn = asyncio.create_task(
            self._run_turn_text(), name="dg-oai-el-dtmf-turn",
        )

    async def on_mark_played(self, name: str) -> None:
        pass

    async def on_saa_prediction(self, event) -> None:
        # Adaptive threshold: if SAA is confident the caller is the
        # device-target, relax; otherwise tighten.
        if self._session is None:
            return
        conf = getattr(event, "confidence", 0.0) or 0.0
        cls = getattr(event, "cls", 0) or 0
        if cls == 2 and conf >= 0.9:
            await self._session.set_threshold(0.62)
        elif conf < 0.3:
            await self._session.set_threshold(0.78)

    async def on_saa_vad(self, event) -> None:
        pass

    async def on_saa_warmup_complete(self) -> None:
        log.info("[dg-oai-el] SAA warmup complete")

    async def on_saa_stats(self, event) -> None:
        pass

    async def on_caller_hangup(self) -> None:
        log.info("[dg-oai-el] caller hung up")

    # ── turn machinery ──────────────────────────────────────────

    async def _run_turn(self, audio: np.ndarray, duration_sec: float) -> None:
        try:
            if self._session is not None:
                await self._session.mark_responding(True)
            transcript = await self._transcribe(audio)
            log.info(
                "[dg-oai-el] caller (%.2fs): %s",
                duration_sec, transcript[:120],
            )
            if not transcript:
                return
            self._history.append({"role": "user", "content": transcript})
            self._trim_history()
            await self._llm_to_tts()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("[dg-oai-el] turn failed")
        finally:
            if self._session is not None:
                await self._session.mark_responding(False)
            self._current_turn = None

    async def _run_turn_text(self) -> None:
        try:
            if self._session is not None:
                await self._session.mark_responding(True)
            await self._llm_to_tts()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("[dg-oai-el] text turn failed")
        finally:
            if self._session is not None:
                await self._session.mark_responding(False)
            self._current_turn = None

    async def _transcribe(self, pcm16_16k: np.ndarray) -> str:
        """Send the PCM16 utterance to Deepgram, return the best transcript."""
        # Build a minimal WAV in-memory so Deepgram's pre-recorded
        # endpoint accepts it without us pulling in another encoder.
        wav_bytes = _pcm16_to_wav(np.ascontiguousarray(pcm16_16k, dtype=np.int16))
        from deepgram import PrerecordedOptions, FileSource

        source: FileSource = {"buffer": wav_bytes, "mimetype": "audio/wav"}
        options = PrerecordedOptions(
            model=self.deepgram_model,
            punctuate=True,
            smart_format=True,
            language="en-US",
        )
        result = await asyncio.to_thread(
            self._dg.listen.rest.v("1").transcribe_file, source, options,
        )
        try:
            return (
                result.results.channels[0]
                .alternatives[0]
                .transcript
                .strip()
            )
        except Exception:  # noqa: BLE001
            return ""

    async def _llm_to_tts(self) -> None:
        """Stream Chat Completions text into ElevenLabs streaming TTS."""
        from elevenlabs.client import AsyncElevenLabs

        el = AsyncElevenLabs(api_key=self.el_key)

        async def token_stream():
            full_reply: list[str] = []
            stream = await self._oai.chat.completions.create(
                model=self.model,
                messages=self._history,
                temperature=self.temperature,
                stream=True,
            )
            async for event in stream:
                if not event.choices:
                    continue
                delta = event.choices[0].delta
                token = getattr(delta, "content", None) or ""
                if token:
                    full_reply.append(token)
                    yield token
            text = "".join(full_reply).strip()
            if text:
                self._history.append({"role": "assistant", "content": text})
                self._trim_history()
                log.info("[dg-oai-el] agent: %s", text[:120])

        # ElevenLabs streams PCM16 @ 16 kHz; that's exactly what the
        # adapter's outbound queue expects, so we forward the bytes
        # without any conversion.
        async for chunk in await el.text_to_speech.stream(
            text=token_stream(),
            voice_id=self.voice_id,
            model_id="eleven_flash_v2_5",
            output_format="pcm_16000",
        ):
            if chunk:
                await self.outbound_pcm16_16k.put(chunk)

    def _trim_history(self) -> None:
        # Keep the system message + the last N exchanges to bound prompt
        # cost and latency. Each turn is two messages (user + assistant).
        cap = 1 + 2 * self.max_history_turns
        if len(self._history) > cap:
            self._history[:] = [self._history[0]] + self._history[-2 * self.max_history_turns:]

    async def _cancel_current_turn(self) -> None:
        task = self._current_turn
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._current_turn = None


def _pcm16_to_wav(pcm16: np.ndarray) -> bytes:
    """Wrap a PCM16 mono @ 16 kHz NumPy array in a minimal WAV header.

    Pure-stdlib so we don't pull in scipy / soundfile just to talk to
    Deepgram's pre-recorded endpoint.
    """
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(pcm16.tobytes())
    return buf.getvalue()


__all__ = ["DeepgramOpenAIElevenLabsBridge"]
