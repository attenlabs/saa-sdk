"""AttentionEngine — consumes saa events from a Daily app-message channel.

Lives inside the customer's Pipecat voice agent. Subscribes to the host
`DailyTransport`'s `on_app_message` event, filters by topic `"saa"`,
parses JSON envelopes + chunked base64 turn payloads into typed events,
and exposes a callback-based API mirroring the SDK's AttentionProcessor.

Upstream actions (mute, set_threshold, ...) are routed back to the hidden
bot participant by constructing a `DailyOutputTransportMessageUrgentFrame`
addressed to `participant_id=agent_pid` and queuing it onto the bound
`PipelineTask` — Pipecat's `DailyTransport` exposes no public
`send_app_message()`, so this frame-queue path is the only supported
send mechanism.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pipecat.transports.daily.transport import (
    DailyOutputTransportMessageUrgentFrame,
    DailyTransport,
)

from . import _wire
from .types import (
    ErrorEvent, InterjectionEvent, InterruptEvent,
    PredictionEvent, TurnReadyEvent, VADEvent,
)


logger = logging.getLogger("saa_pipecat_client.engine")

DATA_TOPIC = "saa"

# Cap on pending chunk-reassembly buffers per session. Bounds memory under
# hostile or buggy producers. Oldest entry is dropped on overflow with an
# error event.
_MAX_PENDING_STREAMS = 10

T = TypeVar("T")
SyncOrAsync = Callable[[T], None] | Callable[[T], Awaitable[None]]
NullaryCallback = Callable[[], None] | Callable[[], Awaitable[None]]


class _PendingStream:
    """In-flight chunked binary payload, keyed by stream_id.

    `envelope` arrives via the `turn_ready` / `interjection` JSON message;
    `chunks` arrive as `turn_chunk` messages carrying base64 data. We don't
    know which order they land in, so both sides stash into here and the
    completion check fires the typed callback once envelope + every chunk
    is present.
    """

    __slots__ = ("kind", "envelope", "chunks", "total_chunks", "byte_len")

    def __init__(self) -> None:
        self.kind: str | None = None             # "turn_ready" | "interjection"
        self.envelope: dict[str, Any] | None = None
        self.chunks: dict[int, bytes] = {}
        self.total_chunks: int | None = None
        self.byte_len: int | None = None

    def is_complete(self) -> bool:
        if self.envelope is None or self.total_chunks is None:
            return False
        return len(self.chunks) >= self.total_chunks

    def assemble(self) -> bytes:
        return b"".join(self.chunks[i] for i in range(self.total_chunks or 0))


class AttentionEngine:
    """Consumes saa app-message events from a Pipecat DailyTransport.

    Construct with the consumer's `DailyTransport` and the `agent_identity`
    returned by `start_attention_session(...)`. Call `await engine.start()`
    to register the app-message handler and wait for the initial `"started"`
    event. Register callbacks via the `@engine.on_*` decorators (sync or
    async functions accepted).

    Lifetime should match the Pipecat pipeline — usually call `start()`
    after constructing the transport and binding the `PipelineTask`, and
    rely on `runner.run(task)` returning to tear everything down.
    Explicit `await engine.stop()` is optional but clears reassembly state.
    """

    def __init__(
        self,
        transport: DailyTransport,
        agent_identity: str,
        *,
        task: Any | None = None,
    ):
        self._transport = transport
        self._agent_identity = agent_identity
        self._task = task

        # Callbacks — set by @on_* decorators
        self._cb_prediction: SyncOrAsync[PredictionEvent] | None = None
        self._cb_vad: SyncOrAsync[VADEvent] | None = None
        self._cb_listening_start: NullaryCallback | None = None
        self._cb_listening_cancelled: NullaryCallback | None = None
        self._cb_turn_ready: SyncOrAsync[TurnReadyEvent] | None = None
        self._cb_interrupt: SyncOrAsync[InterruptEvent] | None = None
        self._cb_interjection: SyncOrAsync[InterjectionEvent] | None = None
        self._cb_error: SyncOrAsync[ErrorEvent] | None = None

        # State
        self._is_ready = False
        self._ready_event = asyncio.Event()
        self._latest_prediction: PredictionEvent | None = None
        self._latest_threshold: float | None = None

        # Pending chunked payloads keyed by stream_id. OrderedDict so we
        # can pop the oldest entry on overflow (FIFO eviction).
        self._pending: OrderedDict[str, _PendingStream] = OrderedDict()

        # Daily participant session id of our hidden bot, resolved when the
        # bot joins. Upstream actions are queued until this lands.
        self._agent_pid: str | None = None
        # Buffer for upstream action messages issued before _agent_pid or
        # _task is resolved. Flushed on resolution.
        self._pending_actions: list[dict[str, Any]] = []

        self._started = False

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self, *, ready_timeout: float = 30.0) -> None:
        """Register the app-message + participant-joined handlers and wait
        for the `"started"` envelope from the hidden bot.

        Raises asyncio.TimeoutError if the hosted bot doesn't emit "started"
        within `ready_timeout` seconds. Common cause: agent_token expired or
        the bot failed to subscribe to the target participant's tracks.
        """
        if self._started:
            return
        self._started = True

        # Pipecat's DailyTransport exposes its event subscriptions via
        # `event_handler("on_app_message")` and `event_handler("on_participant_joined")`.
        # The decorator returns the handler unchanged; we don't need to
        # keep a reference for unregister (Pipecat doesn't expose `.off()`
        # on the transport — stop() just sets a flag to ignore further
        # events).

        @self._transport.event_handler("on_app_message")
        async def _on_app_message(transport: Any, message: Any, sender: Any) -> None:
            if not self._started:
                return
            self._handle_app_message(message, sender)

        @self._transport.event_handler("on_participant_joined")
        async def _on_participant_joined(transport: Any, p: Any) -> None:
            if not self._started:
                return
            self._handle_participant_joined(p)

        await asyncio.wait_for(self._ready_event.wait(), timeout=ready_timeout)

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._pending.clear()
        self._pending_actions.clear()

    def bind_task(self, task: Any) -> None:
        """Set (or replace) the `PipelineTask` used for upstream-action frame
        queueing. Use this when the task is created after the engine.
        Pending upstream actions are flushed once both `task` and
        `_agent_pid` are known.
        """
        self._task = task
        self._maybe_flush_actions()

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """True after the hosted bot's `"started"` event has been received."""
        return self._is_ready

    @property
    def latest_prediction(self) -> PredictionEvent | None:
        """Most recent prediction event (None until first tick lands)."""
        return self._latest_prediction

    @property
    def agent_identity(self) -> str:
        return self._agent_identity

    @property
    def agent_participant_id(self) -> str | None:
        """Daily participant session id of the hidden bot, once resolved."""
        return self._agent_pid

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

    # ── Upstream actions (scoped to the hidden bot) ──────────────────────

    async def mute(self) -> None:
        """Pause feeding mic audio into the hosted processor. The hosted
        bot's ring buffer keeps capturing (for interrupt pre-roll) but
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
        message = {"topic": DATA_TOPIC, **payload}
        if self._task is None or self._agent_pid is None:
            # Buffer until both pid and task are resolved. _maybe_flush_actions
            # runs on every state-change that might make a send possible.
            self._pending_actions.append(message)
            return
        self._dispatch_action_frame(message)

    def _dispatch_action_frame(self, message: dict[str, Any]) -> None:
        """Wrap a JSON action in a DailyOutputTransportMessageUrgentFrame and
        queue it on the bound PipelineTask. This is the only supported send
        path — Pipecat's DailyTransport has no public send_app_message().
        """
        if self._task is None or self._agent_pid is None:
            self._pending_actions.append(message)
            return
        frame = DailyOutputTransportMessageUrgentFrame(
            message=message,
            participant_id=self._agent_pid,
        )
        try:
            result = self._task.queue_frames([frame])
            if asyncio.iscoroutine(result):
                t = asyncio.create_task(result)
                t.add_done_callback(_log_task_exc)
        except Exception:
            logger.exception("failed to queue upstream action frame")

    def _maybe_flush_actions(self) -> None:
        if self._task is None or self._agent_pid is None:
            return
        if not self._pending_actions:
            return
        queued = self._pending_actions
        self._pending_actions = []
        for msg in queued:
            self._dispatch_action_frame(msg)

    def _handle_participant_joined(self, p: Any) -> None:
        """Watch for the hidden bot to join so we can capture its participant
        session id. Pipecat's `on_participant_joined` payload is the raw
        daily-python dict: user metadata sits under `info.userName`, not the
        top-level `userName`.
        """
        try:
            info = p.get("info") if isinstance(p, dict) else None
            user_name = info.get("userName") if isinstance(info, dict) else None
            pid = p.get("id") if isinstance(p, dict) else None
        except Exception:
            logger.exception("malformed participant payload: %r", p)
            return
        if user_name != self._agent_identity:
            return
        if not pid:
            logger.warning("agent participant joined without an id: %r", p)
            return
        self._agent_pid = pid
        logger.debug("resolved agent participant id: %s", pid)
        self._maybe_flush_actions()

    def _handle_app_message(self, message: Any, sender: Any) -> None:
        """Dispatch the topic-tagged app message into the appropriate path.

        The Pipecat callback signature delivers `message` as the raw dict the
        sender passed to `send_app_message` (no wrapping), and `sender` as
        the participant session id string.
        """
        if not isinstance(message, dict):
            return
        if message.get("topic") != DATA_TOPIC:
            return

        evt_type = message.get("type")
        if evt_type == "prediction":
            self._dispatch_prediction(message)
        elif evt_type == "vad":
            self._dispatch_vad(message)
        elif evt_type == "state":
            self._dispatch_state(message)
        elif evt_type == "turn_ready":
            self._dispatch_turn_envelope(message, kind="turn_ready")
        elif evt_type == "interjection":
            self._dispatch_turn_envelope(message, kind="interjection")
        elif evt_type == "turn_chunk":
            self._dispatch_turn_chunk(message)
        elif evt_type == "interrupt":
            self._dispatch_interrupt(message)
        elif evt_type == "started":
            self._is_ready = True
            self._ready_event.set()
        elif evt_type == "config":
            self._latest_threshold = message.get("model_class2_threshold")
        elif evt_type == "error":
            self._dispatch_error(message)
        else:
            # Forward-compat: unknown event types are not fatal.
            logger.debug("unknown event type on saa topic: %s", evt_type)

    def _dispatch_prediction(self, env: dict[str, Any]) -> None:
        ev = PredictionEvent(
            raw_class=int(env.get("class", 0)),
            aligned_class=int(env.get("aligned_class", env.get("class", 0))),
            confidence=float(env.get("confidence") or 0.0),
            source=env.get("source", "model"),
            num_faces=int(env.get("num_faces", 0)),
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

    def _dispatch_turn_envelope(self, env: dict[str, Any], *, kind: str) -> None:
        stream_id = env.get("stream_id")
        if not stream_id:
            logger.warning("%s envelope missing stream_id", kind)
            return
        total = env.get("total_chunks")
        if total is None:
            logger.warning("%s envelope missing total_chunks", kind)
            return
        slot = self._get_or_create_pending(stream_id)
        # Last-writer wins on conflicting envelopes for the same stream_id —
        # the cloud bot is the sole producer so this shouldn't happen.
        slot.kind = kind
        slot.envelope = env
        slot.total_chunks = int(total)
        slot.byte_len = int(env.get("byte_len") or 0)
        self._maybe_complete(stream_id)

    def _dispatch_turn_chunk(self, env: dict[str, Any]) -> None:
        stream_id = env.get("stream_id")
        if not stream_id:
            logger.warning("turn_chunk missing stream_id")
            return
        index = env.get("index")
        b64 = env.get("data_base64")
        if index is None or not isinstance(b64, str):
            logger.warning("turn_chunk malformed for stream %s", stream_id)
            return
        try:
            data = base64.b64decode(b64)
        except Exception:
            logger.warning("turn_chunk base64 decode failed for stream %s", stream_id)
            return
        slot = self._get_or_create_pending(stream_id)
        slot.chunks[int(index)] = data
        self._maybe_complete(stream_id)

    def _get_or_create_pending(self, stream_id: str) -> _PendingStream:
        slot = self._pending.get(stream_id)
        if slot is not None:
            # Touch order so this stream is no longer the oldest.
            self._pending.move_to_end(stream_id)
            return slot
        # Evict oldest entry if at capacity.
        while len(self._pending) >= _MAX_PENDING_STREAMS:
            old_id, _ = self._pending.popitem(last=False)
            logger.warning(
                "dropped pending stream %s — exceeded %d in-flight cap",
                old_id, _MAX_PENDING_STREAMS,
            )
            if self._cb_error is not None:
                _invoke(self._cb_error, ErrorEvent(
                    code="chunk_buffer_overflow",
                    message=f"dropped pending stream {old_id} (cap {_MAX_PENDING_STREAMS})",
                ))
        slot = _PendingStream()
        self._pending[stream_id] = slot
        return slot

    def _maybe_complete(self, stream_id: str) -> None:
        slot = self._pending.get(stream_id)
        if slot is None or not slot.is_complete():
            return
        self._pending.pop(stream_id, None)
        try:
            buf = slot.assemble()
        except Exception:
            logger.exception("failed assembling stream %s", stream_id)
            return
        try:
            parsed = _wire.parse_turn_payload(buf)
        except _wire.TurnPayloadError:
            logger.exception("malformed turn payload on stream %s", stream_id)
            return
        env = slot.envelope or {}
        if slot.kind == "turn_ready":
            self._fire_turn_ready(env, parsed)
        elif slot.kind == "interjection":
            self._fire_interjection(env, parsed)

    def _dispatch_interrupt(self, env: dict[str, Any]) -> None:
        if self._cb_interrupt is None:
            return
        ev = InterruptEvent(confidence=float(env.get("confidence") or 0.0))
        _invoke(self._cb_interrupt, ev)

    def _dispatch_error(self, env: dict[str, Any]) -> None:
        if self._cb_error is None:
            return
        ev = ErrorEvent(
            code=str(env.get("code") or "unknown"),
            message=str(env.get("message") or ""),
        )
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
