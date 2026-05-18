"""TriggerHub — in-process pub/sub for proactive-turn events.

Used by framework overlays to relay back-end ``POST /trigger`` HTTP
webhooks to one or more connected browsers via Server-Sent Events.
Every proactive trigger ends with a back-end HTTP POST, a fan-out to
one-or-more connected browsers (or other subscribers), and a JSON
payload that carries instructions for the agent's opening turn. The
hub is the shared piece.

The hub deliberately does NOT bind to a specific HTTP framework. It
exposes:

* :meth:`TriggerHub.publish` — call from your HTTP POST handler.
* :meth:`TriggerHub.subscribe` — returns an async iterator of events.
* :meth:`TriggerHub.fastapi_router` — optional FastAPI integration
  (only available if FastAPI is installed; ``pip install
  attenlabs-saa-proactive[fastapi]``).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Set


@dataclass
class TriggerEvent:
    """A single proactive-turn trigger payload."""

    instructions: str
    extra: Dict[str, Any]

    def to_json(self) -> str:
        payload: Dict[str, Any] = {"instructions": self.instructions}
        payload.update(self.extra)
        return json.dumps(payload, ensure_ascii=False)


class _Subscriber:
    """One subscriber. Holds an internal queue and a close flag."""

    def __init__(self, parent: "TriggerHub") -> None:
        self._parent = parent
        self._queue: asyncio.Queue[TriggerEvent] = asyncio.Queue(maxsize=64)
        self._closed = False

    def push(self, event: TriggerEvent) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop on overflow; the subscriber is too slow.
            pass

    async def events(self) -> AsyncIterator[TriggerEvent]:
        try:
            while not self._closed:
                event = await self._queue.get()
                if self._closed:
                    return
                yield event
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._parent._unregister(self)

    async def sse_lines(self) -> AsyncIterator[bytes]:
        """Yield Server-Sent Events frames for this subscriber.

        First yields ``: connected\\n\\n`` so reverse proxies don't time
        out, then ``event: trigger\\ndata: {...}\\n\\n`` per event. The
        generator exits when the subscriber is closed (e.g. the HTTP
        client disconnects and the server cancels the response stream).
        """
        yield b": connected\n\n"
        try:
            async for event in self.events():
                yield f"event: trigger\ndata: {event.to_json()}\n\n".encode("utf-8")
        except asyncio.CancelledError:
            return


class TriggerHub:
    """In-process pub/sub for proactive-turn events."""

    def __init__(self) -> None:
        self._subscribers: Set[_Subscriber] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, instructions: str, **extra: Any) -> int:
        """Fan out a trigger to every connected subscriber.

        :returns: the number of subscribers that received the event.
        :raises ValueError: if ``instructions`` is empty or not a string.
        """
        if not isinstance(instructions, str) or not instructions:
            raise ValueError(
                "[saa-proactive] TriggerHub.publish requires a non-empty 'instructions' string"
            )
        event = TriggerEvent(instructions=instructions, extra=dict(extra))
        fanout = 0
        for sub in list(self._subscribers):
            try:
                sub.push(event)
                fanout += 1
            except Exception:
                pass
        return fanout

    def subscribe(self) -> _Subscriber:
        sub = _Subscriber(self)
        self._subscribers.add(sub)
        return sub

    def _unregister(self, sub: _Subscriber) -> None:
        self._subscribers.discard(sub)

    # ── Optional FastAPI integration ────────────────────────────────────────
    def fastapi_router(
        self,
        *,
        trigger_path: str = "/trigger",
        events_path: str = "/trigger-events",
    ) -> Any:
        """Return a FastAPI ``APIRouter`` exposing the standard trigger surface.

        Wire-up::

            from fastapi import FastAPI
            from saa_proactive import TriggerHub

            app = FastAPI()
            hub = TriggerHub()
            app.include_router(hub.fastapi_router())

        Requires ``attenlabs-saa-proactive[fastapi]``.
        """
        try:
            from fastapi import APIRouter, Body, HTTPException  # type: ignore
            from fastapi.responses import StreamingResponse  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "fastapi is required for TriggerHub.fastapi_router(). "
                "Install with: pip install attenlabs-saa-proactive[fastapi]"
            ) from exc

        router = APIRouter()

        @router.post(trigger_path)
        async def _trigger(payload: dict = Body(...)) -> dict:
            instructions = payload.get("instructions")
            if not isinstance(instructions, str) or not instructions.strip():
                raise HTTPException(400, "missing 'instructions' (string)")
            extra = {k: v for k, v in payload.items() if k != "instructions"}
            fanout = self.publish(instructions, **extra)
            if fanout == 0:
                # No subscriber connected to the events endpoint. The
                # operator's webhook should see a hard failure here —
                # silently swallowing the trigger would hide a real
                # misconfiguration (no browser open, SSE stream timed
                # out, etc.). Matches the posture of the reference
                # proactive-agent overlays.
                raise HTTPException(
                    503, f"no subscribers connected to {events_path}"
                )
            return {"ok": True, "subscribers": fanout}

        @router.get(events_path)
        async def _events() -> Any:
            sub = self.subscribe()
            return StreamingResponse(
                sub.sse_lines(), media_type="text/event-stream"
            )

        return router


__all__ = ["TriggerHub", "TriggerEvent"]
