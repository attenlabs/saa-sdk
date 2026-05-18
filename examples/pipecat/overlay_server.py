"""SSE shim that forwards SAA gate decisions to @attenlabs/saa-overlay.

Why this exists
───────────────
The gate emits ``SAADecisionFrame`` / ``SAAPredictionFrame`` /
``SAAStatsFrame`` / ``SAAConnectionFrame`` sidecars on every meaningful
SAA signal. An operator dashboard wants those decisions live without a
full observability stack. This server is the smallest production-quality
way to render them: it stands up a Pipecat ``FrameObserver``, fans the
decision frames out to subscribed SSE clients, and serves the overlay
static page at ``/`` so a deploy is one open in a browser.

Run (split-process model)::

    pip install -r requirements-overlay.txt
    python overlay_server.py           # listens on :8080
    # then in another shell:
    pipecat-runner --transport daily bot:bot

Or in-process: import :class:`OverlayHub`, build the bot's task with
``observers=[OverlayHub.observer()]``, and ``OverlayHub.serve()`` from
the same event loop. See ``run_with_overlay()`` at the bottom of the
file for a working example.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from dotenv import load_dotenv
from loguru import logger

from saa_gate import (
    SAAConnectionFrame,
    SAADecisionFrame,
    SAAPredictionFrame,
    SAAStatsFrame,
)

load_dotenv(override=True)

_OVERLAY_FRAME_TYPES: tuple[type, ...] = (
    SAADecisionFrame,
    SAAPredictionFrame,
    SAAStatsFrame,
    SAAConnectionFrame,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_overlay_event(frame: Any) -> Optional[dict]:
    """Project a SAA sidecar frame into the @attenlabs/saa-overlay schema."""
    if isinstance(frame, SAADecisionFrame):
        return {
            "ts": frame.ts,
            "decision": frame.decision,
            "rule": frame.rule,
            "command_preview": frame.command_preview,
            "reason": frame.reason,
        }
    if isinstance(frame, SAAPredictionFrame):
        bits = [f"cls={frame.cls}", f"conf={frame.confidence:.2f}"]
        if frame.gaze_on_device is not None:
            bits.append(f"gaze={'on' if frame.gaze_on_device else 'off'}")
        if frame.face_visible is not None:
            bits.append(f"face={'y' if frame.face_visible else 'n'}")
        if frame.input_modalities:
            bits.append("+".join(frame.input_modalities))
        return {
            "ts": _now(),
            "decision": "idle",
            "rule": f"saa.prediction.cls{frame.cls}",
            "command_preview": " ".join(bits),
            "reason": f"threshold={frame.threshold:.2f}",
        }
    if isinstance(frame, SAAStatsFrame):
        return {
            "ts": _now(),
            "decision": "idle",
            "rule": "saa.stats",
            "command_preview": (
                f"rtt={frame.rtt_ms:.0f}ms" if frame.rtt_ms is not None else "rtt=?"
            ) + f" audio={frame.sent_audio} video={frame.sent_video}",
            "reason": f"buffered={frame.buffered_amount}B reconnects={frame.reconnect_count}",
        }
    if isinstance(frame, SAAConnectionFrame):
        return {
            "ts": _now(),
            "decision": "override" if frame.state in ("reconnected", "warm") else "idle",
            "rule": f"saa.conn.{frame.state}",
            "command_preview": frame.detail or frame.state,
            "reason": (
                f"code={frame.code} attempt={frame.attempt} delay_ms={frame.delay_ms}"
                if (frame.code is not None or frame.attempt is not None)
                else ""
            ),
        }
    return None


class OverlayHub:
    """Fan-out hub for SAA sidecar frames → SSE subscribers."""

    def __init__(self, *, buffer_size: int = 100) -> None:
        self._buffer_size = max(1, int(buffer_size))
        self._subscribers: list[asyncio.Queue] = []
        self._buffer: deque[dict] = deque(maxlen=self._buffer_size)
        self._lock = asyncio.Lock()

    async def publish_frame(self, frame: Any) -> None:
        event = _to_overlay_event(frame)
        if event is None:
            return
        async with self._lock:
            self._buffer.append(event)
            dead: list[asyncio.Queue] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                if q in self._subscribers:
                    self._subscribers.remove(q)

    async def subscribe(self) -> AsyncIterator[dict]:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._buffer_size * 2)
        async with self._lock:
            for ev in self._buffer:
                q.put_nowait(ev)
            self._subscribers.append(q)
        try:
            while True:
                yield await q.get()
        finally:
            async with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)

    def decision_listener(self):
        """Return a callable to pass to ``SAAGate.add_decision_listener``."""
        async def _listen(frame):
            await self.publish_frame(frame)
        return _listen

    def observer(self):
        """Build a Pipecat ``FrameObserver`` that forwards SAA* frames."""
        try:
            from pipecat.observers.base_observer import BaseObserver, FramePushed
        except ImportError:  # pragma: no cover
            return None

        hub = self

        class _SAAObserver(BaseObserver):
            async def on_push_frame(self, data: FramePushed) -> None:  # type: ignore[override]
                if isinstance(data.frame, _OVERLAY_FRAME_TYPES):
                    await hub.publish_frame(data.frame)

        return _SAAObserver()


OVERLAY_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>SAA overlay · Pipecat</title>
  <link rel="stylesheet" href="https://unpkg.com/@attenlabs/saa-overlay@^0.4/src/overlay.css">
  <style>body{font-family:system-ui;background:#0b1220;color:#e8eef5;margin:0;padding:24px}h1{font-size:18px;font-weight:600;letter-spacing:.02em;margin:0 0 16px}main{max-width:980px;margin:0 auto}</style>
</head>
<body data-theme="dark">
  <main>
    <h1>SAA · Pipecat · live decisions</h1>
    <div id="root"></div>
  </main>
  <script type="module">
    import SaaOverlay from 'https://unpkg.com/@attenlabs/saa-overlay@^0.4/dist/saa-overlay.esm.js';
    SaaOverlay.mount({ container: '#root', source: '/saa/decisions', theme: 'dark' });
  </script>
</body>
</html>
"""


def build_app(hub: OverlayHub):
    """Construct a Starlette app exposing the overlay + SSE endpoint."""
    from sse_starlette.sse import EventSourceResponse
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Route

    async def index(_request):
        return HTMLResponse(OVERLAY_HTML)

    async def healthz(_request):
        return JSONResponse({"ok": True, "buffer": len(hub._buffer)})

    async def decisions(_request):
        async def stream():
            async for event in hub.subscribe():
                yield {"event": "message", "data": json.dumps(event)}
        return EventSourceResponse(stream())

    return Starlette(
        debug=False,
        routes=[
            Route("/", index),
            Route("/healthz", healthz),
            Route("/saa/decisions", decisions),
        ],
    )


def _port() -> int:
    try:
        return int(os.environ.get("SAA_OVERLAY_PORT", "8080"))
    except ValueError:
        return 8080


def _buffer_size() -> int:
    try:
        return int(os.environ.get("SAA_OVERLAY_BUFFER", "100"))
    except ValueError:
        return 100


async def run_with_overlay() -> None:
    """Run the bot + overlay in a single process."""
    import uvicorn

    from bot import _local_transport, run_bot
    from pipecat.runner.types import RunnerArguments

    hub = OverlayHub(buffer_size=_buffer_size())
    app = build_app(hub)

    config = uvicorn.Config(
        app, host="0.0.0.0", port=_port(), log_level="info", lifespan="on"  # noqa: S104
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve(), name="overlay-server")

    transport = _local_transport()
    runner_args = RunnerArguments(handle_sigint=True)
    try:
        await run_bot(
            transport,
            runner_args,
            upstream_mode=False,
            decision_listener=hub.decision_listener(),
        )
    finally:
        server.should_exit = True
        await server_task


def main() -> None:
    import uvicorn

    hub = OverlayHub(buffer_size=_buffer_size())
    app = build_app(hub)
    logger.info(
        "SAA overlay SSE shim listening on http://0.0.0.0:{} (open / for the dashboard, GET /saa/decisions for SSE)",
        _port(),
    )
    uvicorn.run(app, host="0.0.0.0", port=_port(), log_level="info")  # noqa: S104


if __name__ == "__main__":
    main()
