"""OpenAI Realtime bridge — sample-only, NOT part of attenlabs-saa.

Takes base64 PCM16 audio + optional JPEG frames from `saa.AttentionClient`'s
`turn_ready` event, forwards them to OpenAI's Realtime API (audio +
input_image content parts), and plays the response back through the local
speaker via simpleaudio.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time
from typing import Callable, Optional

import numpy as np
import simpleaudio as sa
import sounddevice as sd
import websocket

REALTIME_BASE = "wss://api.openai.com/v1/realtime"
DEFAULT_MODEL = "gpt-realtime-2"
DEFAULT_REASONING_EFFORT = "minimal"
DEFAULT_VOICE = "sage"
OPENAI_OUTPUT_RATE = 24000   # OpenAI Realtime emits at 24 kHz

# if true, stream using sounddevice as audio deltas arrive
STREAMING_PLAYBACK = True
PLAYBACK_GAIN = 10 ** (6 / 20.0)  # +6 dB, matches the buffered path
logger = logging.getLogger("saa_demo.llm")


def _b64_to_float32(chunk_b64: str) -> np.ndarray:
    pcm_bytes = base64.b64decode(chunk_b64)
    return np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0


class _StreamPlayer:
    """Gapless streaming playback: a thread blocking-writes int16 chunks to a
    sounddevice OutputStream as they arrive. on_start fires on the first chunk,
    on_end once the buffer drains after input is done (or on interrupt)."""

    def __init__(self, rate: int, on_start: Callable, on_end: Callable):
        self._rate = rate
        self._on_start = on_start
        self._on_end = on_end
        self._q: queue.Queue = queue.Queue()
        self._stream = None
        self._thread = None
        self._aborted = False
        self._emit_end = True
        self._ended = False
        self._lock = threading.Lock()

    def add(self, pcm16: np.ndarray) -> None:
        if self._aborted:
            return
        if self._thread is None:
            self._stream = sd.OutputStream(samplerate=self._rate, channels=1, dtype="int16")
            self._stream.start()
            self._thread = threading.Thread(target=self._run, daemon=True, name="llm-playback")
            self._thread.start()
            self._on_start()
        self._q.put(pcm16)

    def input_done(self) -> None:
        if self._ended or self._aborted:
            return
        if self._thread is None:
            self._finish()       # no audio ever arrived
        else:
            self._q.put(None)    # sentinel: drain then end

    def interrupt(self) -> None:
        if self._thread is None:
            self._finish()
        else:
            self._abort()        # _run's finally -> _finish -> on_end

    def stop(self) -> None:
        # Anti-overlap replace: hard-stop without on_end.
        self._emit_end = False
        if self._thread is None:
            self._ended = True
        else:
            self._abort()

    def _abort(self) -> None:
        self._aborted = True
        if self._stream is not None:
            try:
                self._stream.abort()
            except Exception:
                pass
        self._q.put(None)

    def _run(self) -> None:
        try:
            while True:
                chunk = self._q.get()
                if chunk is None or self._aborted:
                    break
                self._stream.write(chunk)
        except Exception:
            logger.exception("llm streaming playback error")
        finally:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._finish()

    def _finish(self) -> None:
        with self._lock:
            if self._ended:
                return
            self._ended = True
            emit = self._emit_end
        if emit:
            self._on_end()


class RealtimeLLMBridge:
    def __init__(
        self,
        api_key: str,
        *,
        url: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        voice: str = DEFAULT_VOICE,
        instructions: str = "You are a helpful assistant.",
        temperature: float = 0.8,
    ):
        if not api_key:
            raise ValueError("RealtimeLLMBridge: api_key required")
        self.api_key = api_key
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.url = url or f"{REALTIME_BASE}?model={model}"
        self.voice = voice
        self.instructions = instructions
        self.temperature = temperature

        self.ws: Optional[websocket.WebSocketApp] = None
        self.ws_thread: Optional[threading.Thread] = None
        self.session_ready = False
        self.audio_chunks: list[str] = []
        self.pending_audio: Optional[str] = None
        self.pending_frames: list = []
        self.response_timer: Optional[float] = None
        self.closed = False

        # OpenAI's response id, set on response.created and cleared on
        # response.done. interrupt() only sends response.cancel when this is
        # set
        self._active_response_id: Optional[str] = None
        self._response_created_at: Optional[float] = None
        self._first_audio_at: Optional[float] = None
        self._has_connected = False

        # True between interrupt() and the next send_audio_b64.
        self._suppress_next_playback = False

        # simpleaudio PlayObject for the currently-playing response, so
        # interrupt() can hard-stop it
        self._active_play_obj: Optional[sa.PlayObject] = None
        self._active_play_lock = threading.Lock()

        self._player: Optional[_StreamPlayer] = None

        self._listeners: dict[str, list[Callable]] = {}

    def on(self, event: str, func: Callable) -> Callable:
        self._listeners.setdefault(event, []).append(func)
        return func

    def _emit(self, event: str, *args) -> None:
        for fn in self._listeners.get(event, []):
            try:
                fn(*args)
            except Exception:
                logger.exception("llm listener '%s' raised", event)

    def send_audio_b64(self, audio_b64: str, frames=None) -> None:
        """Send the next user turn.

        frames: optional list of objects exposing `image_base64` (str, raw
        JPEG base64, no data: prefix). Accepts saa.TurnFrame, dicts, or any
        duck-typed equivalent so this stays LLM-bridge-only.
        """
        self.pending_audio = audio_b64
        self.pending_frames = list(frames) if frames else []
        self.closed = False
        # New user turn — clear interrupt suppression so the next response plays.
        self._suppress_next_playback = False
        if self.session_ready and self._ws_open():
            self._flush()
            return
        self._connect()

    def _ws_open(self) -> bool:
        ws = self.ws
        if ws is None:
            return False
        sock = getattr(ws, "sock", None)
        return bool(sock and getattr(sock, "connected", False))

    def _connect(self) -> None:
        if self.ws_thread is not None and self.ws_thread.is_alive():
            return

        if self._has_connected:
            logger.warning(
                "[llm] opening new LLM session — conversation context will "
                "reset (prior socket dropped)"
            )
            self._emit("session_reset", {"cause": "reconnect"})
        self._has_connected = True
        self.session_ready = False
        headers = [
            f"Authorization: Bearer {self.api_key}",
        ]
        self.ws = websocket.WebSocketApp(
            self.url,
            header=headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_close=self._on_close,
            on_error=self._on_error,
        )
        self.ws_thread = threading.Thread(
            target=self.ws.run_forever, daemon=True, name="llm-ws",
        )
        self.ws_thread.start()

    def _on_open(self, ws) -> None:
        session = {
            "type": "realtime",
            "model": self.model,
            "output_modalities": ["audio"],
            "instructions": self.instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "turn_detection": None,
                    "transcription": {"model": "whisper-1"},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": self.voice,
                },
            },
            "tool_choice": "auto",
            "max_output_tokens": "inf",
        }

        if self.reasoning_effort:
            session["reasoning"] = {"effort": self.reasoning_effort}
        ws.send(json.dumps({"type": "session.update", "session": session}))

    def _on_message(self, ws, message) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        t = data.get("type")
        # Only flush after session.updated — session.created carries the
        # server-default config, not our session.update.
        if t == "session.updated":
            if not self.session_ready:
                self.session_ready = True
                self._flush()
        elif t == "session.created":
            pass
        elif t == "response.created":
            # OpenAI response lifecycle starts here
            resp = data.get("response") or {}
            self._active_response_id = str(resp.get("id") or "unknown")
            self._response_created_at = time.monotonic()
            self._first_audio_at = None

            if STREAMING_PLAYBACK:
                self._stream_start()
            else:
                self.audio_chunks = []
        elif t in ("response.audio.delta", "response.output_audio.delta"):
            delta = data.get("delta")
            if delta:
                if self._first_audio_at is None:
                    self._first_audio_at = time.monotonic()
                if STREAMING_PLAYBACK:
                    self._stream_chunk(delta)
                else:
                    self.audio_chunks.append(delta)
        elif t in ("response.audio.done", "response.output_audio.done"):
            pass
        elif t in ("response.audio_transcript.done",
                   "response.output_audio_transcript.done"):
            self._emit("transcript", data.get("transcript", ""))
        elif t == "response.done":
            # End of the whole response. Status can be: completed,
            # cancelled, failed, incomplete.
            resp = data.get("response") or {}
            status = resp.get("status")
            self._active_response_id = None
            self._log_turn_timing()
            if status == "failed":
                err = (resp.get("status_details") or {}).get("error") or {}
                self._emit("error", {
                    "title": "LLM Response Failed",
                    "message": err.get("message") or json.dumps(resp),
                })
                if STREAMING_PLAYBACK:
                    self._stream_interrupt()
                else:
                    self.audio_chunks = []
                    self._emit("speaking_end")
            elif STREAMING_PLAYBACK:
                self._stream_input_done()
            elif self.audio_chunks:
                self._playback()
            else:
                # Response ended with no audio buffered, so end the turn
                self._emit("speaking_end")
        elif t == "error":
            err = data.get("error") or {}
            if err.get("code") == "response_cancel_not_active":
                logger.debug("response.cancel raced response.done — ignored")
                return
            self._emit("error", {
                "title": "LLM Error",
                "message": err.get("message") or str(data),
            })
            self._emit("speaking_end")

    def _log_turn_timing(self) -> None:
        """latency breakdown for one response, logged at response.done.
        Intervals: sent->created = LLM ack (response.created), sent->first_audio
        = time to first audio token, first_audio->done = how long audio sat
        before we play it
        """
        sent = self.response_timer
        if sent is None:
            return
        now = time.monotonic()
        created = self._response_created_at
        first = self._first_audio_at
        ack_ms = (created - sent) * 1000.0 if created else float("nan")
        ttfb_ms = (first - sent) * 1000.0 if first else float("nan")
        buffered_ms = (now - first) * 1000.0 if first else 0.0
        total_ms = (now - sent) * 1000.0
        logger.info(
            "[llm-timing] sent->created=%.0fms ttfb(sent->first_audio)=%.0fms "
            "buffered(first_audio->done)=%.0fms total=%.0fms "
            "(Option-A headroom ≤%.0fms)",
            ack_ms, ttfb_ms, buffered_ms, total_ms, buffered_ms,
        )
        self.response_timer = None
        self._response_created_at = None
        self._first_audio_at = None

    def _on_close(self, ws, code, reason) -> None:
        self.session_ready = False
        self.ws = None
        if not self.closed:
            logger.warning(
                "[llm] socket closed: code=%s reason=%s — conversation context "
                "will reset on next turn", code, reason or "none",
            )
        if self.pending_audio and not self.closed:
            self.pending_audio = None
            self.pending_frames = []
            self._emit("error", {
                "title": "LLM Disconnected",
                "message": "LLM connection dropped mid-request.",
            })
            self._emit("speaking_end")

    def _on_error(self, ws, error) -> None:
        logger.debug("llm ws error: %s", error)

    def _flush(self) -> None:
        if not self.pending_audio or not self._ws_open():
            return
        audio = self.pending_audio
        frames = self.pending_frames
        self.pending_audio = None
        self.pending_frames = []
        self.response_timer = time.monotonic()

        content = []
        for f in frames:
            b64 = getattr(f, "image_base64", None)
            if b64 is None and isinstance(f, dict):
                b64 = f.get("image_base64")
            if not b64:
                continue
            content.append({
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{b64}",
            })
        content.append({"type": "input_audio", "audio": audio})
        try:
            self.ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "user", "content": content},
            }))
            self.ws.send(json.dumps({"type": "response.create"}))
        except Exception as e:
            self._emit("error", {"title": "LLM Send Error", "message": str(e)})
            self._emit("speaking_end")

    def _stream_start(self) -> None:
        if self._player is not None:
            self._player.stop()
        self._player = _StreamPlayer(
            OPENAI_OUTPUT_RATE,
            lambda: self._emit("speaking_start"),
            lambda: self._emit("speaking_end"),
        )

    def _stream_chunk(self, delta: str) -> None:
        if self._player is None:
            return
        samples = np.clip(_b64_to_float32(delta) * PLAYBACK_GAIN, -1.0, 1.0)
        self._player.add((samples * 32767.0).astype(np.int16))

    def _stream_input_done(self) -> None:
        if self._player is not None:
            self._player.input_done()
        else:
            self._emit("speaking_end")

    def _stream_interrupt(self) -> None:
        if self._player is not None:
            self._player.interrupt()
        else:
            self._emit("speaking_end")

    def _playback(self) -> None:
        chunks = self.audio_chunks
        self.audio_chunks = []

        # Cancelled response audio arriving after we've already interrupted
        if self._suppress_next_playback:
            self._suppress_next_playback = False
            logger.debug("suppressed stale playback (post-interrupt)")
            self._emit("speaking_end")
            return

        if not chunks:
            self._emit("speaking_end")
            return

        combined = np.concatenate([_b64_to_float32(c) for c in chunks])
        if combined.size == 0:
            self._emit("speaking_end")
            return
        out = combined * (10 ** (6 / 20.0))

        if self.response_timer is not None:
            dt = time.monotonic() - self.response_timer
            logger.info("llm response time: %.2fs", dt)

        # if a previous response is still playing, stop it
        self._stop_active_playback()

        self._emit("speaking_start")

        # Convert to int16 PCM since simpleaudio expects byte-level samples.
        # Clip first so float values outside [-1, 1] don't wrap around.
        clipped = np.clip(out, -1.0, 1.0)
        pcm16 = (clipped * 32767.0).astype(np.int16)

        def play():
            try:
                play_obj = sa.play_buffer(pcm16, 1, 2, OPENAI_OUTPUT_RATE)
                with self._active_play_lock:
                    self._active_play_obj = play_obj
                while play_obj.is_playing():
                    time.sleep(0.01)
            except Exception:
                logger.exception("llm playback error")
            finally:
                with self._active_play_lock:
                    if self._active_play_obj is play_obj:
                        self._active_play_obj = None
                self._emit("speaking_end")

        threading.Thread(target=play, daemon=True, name="llm-playback").start()

    def _stop_active_playback(self) -> None:
        """Hard-stop any in-flight simpleaudio playback. Safe when nothing
        is active. simpleaudio's PlayObject has no gain ramp; the
        ``fade_ms`` arg on ``interrupt()`` is honored by JS bridges with
        Web Audio but is effectively "stop now" here."""
        with self._active_play_lock:
            play_obj = self._active_play_obj
            self._active_play_obj = None
        if play_obj is not None:
            try:
                play_obj.stop()
            except Exception:
                logger.debug("stop on PlayObject raised", exc_info=True)

    def interrupt(self, fade_ms: int = 500) -> None:
        """Stop playback and (when a response is in flight) cancel the
        upstream OpenAI generation

        ``fade_ms`` is accepted for API parity with the JS bridge but
        simpleaudio doesn't expose so playback hard-stops.
        """
        if STREAMING_PLAYBACK:
            if self._active_response_id and self._ws_open():
                try:
                    self.ws.send(json.dumps({"type": "response.cancel"}))
                except Exception:
                    logger.debug("response.cancel send failed", exc_info=True)
            self._stream_interrupt()
            return

        # Drop any buffered chunks and suppress the next stale audio.done.
        self.audio_chunks = []
        self._suppress_next_playback = True

        # Cancel upstream — only if a response is still in flight. Once
        # response.done has landed, OpenAI rejects response.cancel with
        # response_cancel_not_active.
        if self._active_response_id and self._ws_open():
            try:
                self.ws.send(json.dumps({"type": "response.cancel"}))
            except Exception:
                logger.debug("response.cancel send failed", exc_info=True)

        # Stop local playback. The play thread's finally block will emit
        # speaking_end. If nothing was playing, emit directly so the consumer
        # unwinds responding state.
        with self._active_play_lock:
            had_active = self._active_play_obj is not None
        self._stop_active_playback()
        if not had_active:
            self._emit("speaking_end")

    def close(self) -> None:
        self.closed = True
        self.pending_audio = None
        self.pending_frames = []
        self._suppress_next_playback = False
        self._active_response_id = None
        self.audio_chunks = []
        self._stop_active_playback()
        if self._player is not None:
            self._player.stop()
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
