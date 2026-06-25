"""AttentionEngine — consumes saa events from the LiveKit data channel.

Lives inside the customer's voice agent entrypoint. Listens on the "saa"
topic, parses JSON envelopes + binary turn payloads into typed events, and
exposes a callback-based API mirroring the SDK's AttentionProcessor.

Upstream actions (mute, set_threshold, ...) are routed back to the hidden
agent participant via scoped `publish_data(destination_identities=[...])`,
so the events never leak to other room participants.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from livekit import rtc

from . import _wire
from .types import (
    ErrorEvent, InterjectionEvent, InterruptEvent,
    PredictionEvent, TurnReadyEvent, VADEvent,
)


logger = logging.getLogger("saa_livekit_client.engine")

DATA_TOPIC = "saa"

T = TypeVar("T")
SyncOrAsync = Callable[[T], None] | Callable[[T], Awaitable[None]]
NullaryCallback = Callable[[], None] | Callable[[], Awaitable[None]]


class AttentionStartupError(RuntimeError):
    """Raised by `start()` when the hosted agent reports an error before it
    becomes ready — fails fast with the reason instead of a blind ready_timeout.
    """

    def __init__(self, code: str, message: str):
        super().__init__(f"saa startup error [{code}]: {message}")
        self.code = code
        self.message = message


class AttentionEngine:
    """Consumes saa data-channel events from a LiveKit room.

    Construct with the customer's room object and the agent_identity returned
    by `start_attention_session(...)`. Call `await engine.start()` to register
    handlers and wait for the initial `"started"` event. Register callbacks
    via the `@engine.on_*` decorators (sync or async functions accepted).

    Lifetime should match the LiveKit session — usually call `start()` after
    `ctx.connect()` and rely on the room's disconnect to tear down. Explicit
    `await engine.stop()` is optional but cleans up byte-stream handlers.
    """

    def __init__(self, room: rtc.Room, agent_identity: str):
        self._room = room
        self._agent_identity = agent_identity

        # Callbacks — set by @on_* decorators
        self._cb_prediction: SyncOrAsync[PredictionEvent] | None = None
        self._cb_vad: SyncOrAsync[VADEvent] | None = None
        self._cb_listening_start: NullaryCallback | None = None
        self._cb_listening_cancelled: NullaryCallback | None = None
        self._cb_turn_ready: SyncOrAsync[TurnReadyEvent] | None = None
        self._cb_interrupt: SyncOrAsync[InterruptEvent] | None = None
        self._cb_interjection: SyncOrAsync[InterjectionEvent] | None = None
        self._cb_error: SyncOrAsync[ErrorEvent] | None = None
        self._cb_warmup: NullaryCallback | None = None

        # State
        self._is_ready = False
        self._ready_event = asyncio.Event()
        # An error event arriving before "started" lands here and wakes start().
        self._startup_error: ErrorEvent | None = None
        self._latest_prediction: PredictionEvent | None = None
        self._latest_threshold: float | None = None

        # Pending turn envelopes keyed by stream_id. The byte stream typically
        # arrives just after the JSON envelope but ordering isn't guaranteed.
        self._pending_turns: dict[str, dict[str, Any]] = {}
        # Pending interjection envelopes — same shape, separate dict so we
        # don't mix types when matching stream_id → typed event.
        self._pending_interjections: dict[str, dict[str, Any]] = {}

        self._data_handler: Callable | None = None
        self._started = False

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self, *, ready_timeout: float = 30.0) -> None:
        """Register data + byte-stream handlers, wait for the "started" event.

        Fails with AttentionStartupError if the hosted agent publishes an
        error before it becomes ready. Raises asyncio.TimeoutError if the agent
        doesn't emit "started" within `ready_timeout` seconds. 
        
        Common cause: agent_token expired or the agent failed to subscribe to the target
        participant's tracks.
        """
        if self._started:
            return
        self._started = True

        self._data_handler = self._on_data_received
        self._room.on("data_received", self._data_handler)

        try:
            self._room.register_byte_stream_handler(DATA_TOPIC, self._on_byte_stream)
        except Exception as e:
            # If the customer's livekit-client version doesn't have the
            # byte-stream API, turn_ready payloads will silently drop frames.
            # Don't make this fatal — predictions still flow.
            logger.warning(
                "byte_stream_handler not registered (%s) — "
                "turn_ready frames will be unavailable", e,
            )

        await asyncio.wait_for(self._ready_event.wait(), timeout=ready_timeout)
        if self._startup_error is not None:
            raise AttentionStartupError(
                self._startup_error.code, self._startup_error.message,
            )

    async def stop(self) -> None:
        if not self._started:
            return
        if self._data_handler is not None:
            try:
                self._room.off("data_received", self._data_handler)
            except Exception:
                pass
            self._data_handler = None
        try:
            self._room.unregister_byte_stream_handler(DATA_TOPIC)
        except Exception:
            pass
        self._started = False

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """True after the hosted agent's `"started"` event has been received."""
        return self._is_ready

    @property
    def latest_prediction(self) -> PredictionEvent | None:
        """Most recent prediction event (None until first tick lands)."""
        return self._latest_prediction

    @property
    def agent_identity(self) -> str:
        return self._agent_identity

    # ── Callback registration (decorator form) ───────────────────────────

    def on_prediction(self, fn: SyncOrAsync[PredictionEvent]) -> SyncOrAsync[PredictionEvent]:
        self._cb_prediction = fn
        return fn

    def on_vad(self, fn: SyncOrAsync[VADEvent]) -> SyncOrAsync[VADEvent]:
        self._cb_vad = fn
        return fn

    def on_listening_start(self, fn: NullaryCallback) -> NullaryCallback:
        self._cb_listening_start = fn
        return fn

    def on_listening_cancelled(self, fn: NullaryCallback) -> NullaryCallback:
        self._cb_listening_cancelled = fn
        return fn

    def on_turn_ready(self, fn: SyncOrAsync[TurnReadyEvent]) -> SyncOrAsync[TurnReadyEvent]:
        self._cb_turn_ready = fn
        return fn

    def on_interrupt(self, fn: SyncOrAsync[InterruptEvent]) -> SyncOrAsync[InterruptEvent]:
        self._cb_interrupt = fn
        return fn

    def on_interjection(self, fn: SyncOrAsync[InterjectionEvent]) -> SyncOrAsync[InterjectionEvent]:
        self._cb_interjection = fn
        return fn

    def on_error(self, fn: SyncOrAsync[ErrorEvent]) -> SyncOrAsync[ErrorEvent]:
        self._cb_error = fn
        return fn

    def on_warmup(self, fn: NullaryCallback) -> NullaryCallback:
        """fn() — fires once when the model is warmed up and producing its
        first real prediction. Distinct from `is_ready` / `started`, which
        only mean the session is established and the model is loaded."""
        self._cb_warmup = fn
        return fn

    # ── Upstream actions (scoped to the hidden agent) ────────────────────

    async def mute(self) -> None:
        """Pause feeding mic audio into the hosted processor. The hosted
        agent's ring buffer keeps capturing (for interrupt pre-roll) but
        chunk_accumulator stops growing."""
        await self._send_action({"action": "mute"})

    async def unmute(self) -> None:
        await self._send_action({"action": "unmute"})

    async def responding_start(self) -> None:
        """Signal the hosted processor that the AI is now speaking back.
        Activates the InterruptDetector branch; suppresses InterjectionDetector.
        """
        await self._send_action({"action": "responding_start"})

    async def responding_stop(self) -> None:
        await self._send_action({"action": "responding_stop"})

    async def set_threshold(self, value: float) -> None:
        """Update the model's class-2 confidence threshold. Affects both the
        state machine's LISTENING entry and the UI confidence bar.
        """
        v = max(0.0, min(1.0, float(value)))
        await self._send_action({"action": "set_threshold", "value": v})

    # ── Internals ────────────────────────────────────────────────────────

    async def _send_action(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        await self._room.local_participant.publish_data(
            data,
            reliable=True,
            topic=DATA_TOPIC,
            destination_identities=[self._agent_identity],
        )

    def _on_data_received(self, packet: rtc.DataPacket) -> None:
        """LiveKit data_received handler. Filters topic, then dispatches.

        Stays synchronous because livekit-rtc invokes the handler on the
        event loop already; we fan out to async via asyncio.create_task.
        """
        if packet.topic != DATA_TOPIC:
            return
        participant = packet.participant
        if participant is not None and participant.identity != self._agent_identity:
            # Non-hidden sender on our topic — must match our agent.
            return

        try:
            envelope = json.loads(packet.data.decode("utf-8"))
        except Exception:
            logger.warning("malformed JSON on saa topic: %r", packet.data[:80])
            return

        evt_type = envelope.get("type")
        logger.debug("recv %s %s", evt_type, {k: v for k, v in envelope.items() if k != "type"})
        if evt_type == "prediction":
            self._dispatch_prediction(envelope)
        elif evt_type == "vad":
            self._dispatch_vad(envelope)
        elif evt_type == "state":
            self._dispatch_state(envelope)
        elif evt_type == "turn_ready":
            self._dispatch_turn_ready_envelope(envelope)
        elif evt_type == "interrupt":
            self._dispatch_interrupt(envelope)
        elif evt_type == "interjection":
            self._dispatch_interjection_envelope(envelope)
        elif evt_type == "started":
            self._is_ready = True
            self._ready_event.set()
        elif evt_type == "warmup_complete":
            # model produced its first real prediction (warmed up + predicting)
            if self._cb_warmup is not None:
                _invoke_nullary(self._cb_warmup)
        elif evt_type == "config":
            self._latest_threshold = envelope.get("model_class2_threshold")
        elif evt_type == "error":
            self._dispatch_error(envelope)
        else:
            # Forward-compat: unknown event types are not fatal.
            logger.debug("unknown event type on saa topic: %s", evt_type)

    def _on_byte_stream(self, reader: Any, sender_identity: str) -> None:
        """Byte-stream handler for binary turn payloads.

        livekit-rtc signature: `(reader: ByteStreamReader, participant_identity: str)`.
        The stream id + topic + mime live on `reader.info`; the participant
        identity is a plain string

        Receives the per-turn PCM + JPEGs, looks up the matching envelope
        by stream_id, and fires the typed callback once both are in hand.
        The actual read is async; schedule as a task so the handler
        returns fast.
        """
        # Filter by sender for safety, though topic filtering already scopes us.
        if sender_identity and sender_identity != self._agent_identity:
            return

        info = getattr(reader, "info", None)
        stream_id = getattr(info, "stream_id", None) if info is not None else None
        if not stream_id:
            logger.warning(
                "byte stream from %s on topic %s missing stream_id — dropping",
                sender_identity,
                getattr(info, "topic", "?") if info is not None else "?",
            )
            return

        asyncio.create_task(self._read_byte_stream(reader, stream_id))

    async def _read_byte_stream(self, reader: Any, stream_id: str) -> None:
        try:
            # ByteStreamReader implements __aiter__ + __anext__, yielding
            # raw bytes chunks until the stream closes. There's no read_all().
            chunks: list[bytes] = []
            async for chunk in reader:
                chunks.append(chunk)
            buf = b"".join(chunks)
        except Exception as e:
            # unexpected transport/read failure (rare) — keep the traceback
            logger.warning(
                "failed reading turn byte stream %s: %s", stream_id, e, exc_info=True,
            )
            return

        # The byte-stream reader yields chunks in arrival order and does NOT
        # reorder by chunk_index; if the transport delivers a payload short or
        # out of order (observed on some Windows clients), the buffer is
        # misframed and parse_turn_payload would read garbage frame lengths.
        # When the sender declared total_length, validate it up front and drop
        # the turn with one clear log line instead of misparsing into a flood
        # of TurnPayloadError tracebacks.
        expected = getattr(getattr(reader, "info", None), "size", None)
        if expected and len(buf) != expected:
            logger.warning(
                "incomplete turn stream %s: got %d bytes, expected %d — dropping",
                stream_id, len(buf), expected,
            )
            return

        try:
            parsed = _wire.parse_turn_payload(buf)
        except _wire.TurnPayloadError as e:
            logger.warning(
                "dropping malformed turn payload on stream %s (%d bytes): %s",
                stream_id, len(buf), e,
            )
            return

        # Match against whichever envelope is waiting. turn_ready and
        # interjection share the binary format; envelopes go into separate
        # dicts so we know which typed callback to fire.
        env = self._pending_turns.pop(stream_id, None)
        if env is not None:
            self._fire_turn_ready(env, parsed)
            return
        env = self._pending_interjections.pop(stream_id, None)
        if env is not None:
            self._fire_interjection(env, parsed)
            return
        # Envelope hasn't arrived yet — stash the parsed payload by stream_id
        # so the envelope handler can match. Two dicts to avoid type mixing.
        self._pending_turns[stream_id] = {"_orphan_payload": parsed}

    def _dispatch_prediction(self, env: dict[str, Any]) -> None:
        ev = PredictionEvent(
            raw_class=int(env.get("class", 0)),
            aligned_class=int(env.get("aligned_class", env.get("class", 0))),
            confidence=float(env.get("confidence") or 0.0),
            source=env.get("source", "model"),
            num_faces=int(env.get("num_faces", 0)),
            responding=bool(env.get("responding", env.get("source") == "ai_responding")),
        )
        self._latest_prediction = ev
        if self._cb_prediction is not None:
            _invoke(self._cb_prediction, ev)

    def _dispatch_vad(self, env: dict[str, Any]) -> None:
        if self._cb_vad is None:
            return
        ev = VADEvent(
            is_speech=bool(env.get("is_speech", False)),
            probability=float(env.get("probability") or 0.0),
        )
        _invoke(self._cb_vad, ev)

    def _dispatch_state(self, env: dict[str, Any]) -> None:
        state = env.get("state")
        if state == "listening" and self._cb_listening_start is not None:
            _invoke_nullary(self._cb_listening_start)
        elif state == "cancelled" and self._cb_listening_cancelled is not None:
            _invoke_nullary(self._cb_listening_cancelled)

    def _dispatch_turn_ready_envelope(self, env: dict[str, Any]) -> None:
        stream_id = env.get("stream_id")
        if not stream_id:
            logger.warning("turn_ready envelope missing stream_id")
            return
        # If the byte stream beat us here, the orphan payload is waiting.
        orphan = self._pending_turns.pop(stream_id, None)
        if orphan and "_orphan_payload" in orphan:
            self._fire_turn_ready(env, orphan["_orphan_payload"])
            return
        # Otherwise stash the envelope for the byte stream handler to match.
        self._pending_turns[stream_id] = env

    def _dispatch_interjection_envelope(self, env: dict[str, Any]) -> None:
        stream_id = env.get("stream_id")
        if not stream_id:
            logger.warning("interjection envelope missing stream_id")
            return
        orphan = self._pending_turns.pop(stream_id, None)
        if orphan and "_orphan_payload" in orphan:
            self._fire_interjection(env, orphan["_orphan_payload"])
            return
        self._pending_interjections[stream_id] = env

    def _dispatch_interrupt(self, env: dict[str, Any]) -> None:
        if self._cb_interrupt is None:
            return
        ev = InterruptEvent(confidence=float(env.get("confidence") or 0.0))
        _invoke(self._cb_interrupt, ev)

    def _dispatch_error(self, env: dict[str, Any]) -> None:
        ev = ErrorEvent(
            code=str(env.get("code") or "unknown"),
            message=str(env.get("message") or ""),
        )
        # An error before the "started" handshake aborts start() fast with the
        # reason instead of letting it block until ready_timeout.
        if not self._is_ready and self._startup_error is None:
            self._startup_error = ev
            self._ready_event.set()
        if self._cb_error is not None:
            _invoke(self._cb_error, ev)

    def _fire_turn_ready(self, env: dict[str, Any], parsed: _wire.ParsedTurnPayload) -> None:
        if self._cb_turn_ready is None:
            return
        ev = TurnReadyEvent(
            audio_pcm16=parsed.pcm16,
            duration=float(env.get("duration") or 0.0),
            frames=parsed.frames,
            context=env.get("context"),
        )
        _invoke(self._cb_turn_ready, ev)

    def _fire_interjection(self, env: dict[str, Any], parsed: _wire.ParsedTurnPayload) -> None:
        if self._cb_interjection is None:
            return
        ev = InterjectionEvent(
            reason=env.get("reason", "stuck_after_question"),
            audio_pcm16=parsed.pcm16,
            duration=float(env.get("duration") or 0.0),
        )
        _invoke(self._cb_interjection, ev)


def _invoke(fn: SyncOrAsync, arg: Any) -> None:
    """Invoke either sync or async callback. Async callbacks are scheduled
    on the running loop and their exceptions are logged via task done callback.
    """
    try:
        result = fn(arg)
    except Exception:
        logger.exception("callback raised")
        return
    if asyncio.iscoroutine(result):
        task = asyncio.create_task(result)
        task.add_done_callback(_log_task_exc)


def _invoke_nullary(fn: NullaryCallback) -> None:
    try:
        result = fn()
    except Exception:
        logger.exception("callback raised")
        return
    if asyncio.iscoroutine(result):
        task = asyncio.create_task(result)
        task.add_done_callback(_log_task_exc)


def _log_task_exc(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.exception("callback task raised", exc_info=exc)
