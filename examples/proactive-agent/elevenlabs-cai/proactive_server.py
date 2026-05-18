"""Proactive overlay on examples/elevenlabs-cai/server.py.

Reuses the parent server's FastAPI ``app`` (mints ElevenLabs signed
URLs, holds ``xi-api-key`` server-side, serves static files) and adds
the same proactive-trigger surface used by the OpenAI Realtime
variant:

* ``POST /proactive-trigger`` for back-end webhooks.
* ``GET /proactive-events`` Server-Sent Events stream the browser
  subscribes to.

The browser-side ``proactive.js`` asserts ``markResponding(true)`` and
calls ``convo.sendUserMessage`` to force the agent to speak first.
"""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import sys
from typing import AsyncIterator


_PARENT = pathlib.Path(__file__).resolve().parent.parent.parent / "elevenlabs-cai"
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from server import app  # noqa: E402

try:
    from fastapi import Body, HTTPException
    from fastapi.responses import FileResponse, StreamingResponse
except ImportError as exc:  # pragma: no cover
    raise SystemExit("fastapi required: pip install -r requirements.txt") from exc


log = logging.getLogger("saa.proactive_agent.elevenlabs_cai")
_subscribers: list[asyncio.Queue[str]] = []


@app.post("/proactive-trigger")
async def proactive_trigger(payload: dict = Body(...)) -> dict:
    """Fire a proactive turn at every connected browser."""
    instructions = payload.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise HTTPException(400, "missing 'instructions' (string)")
    event = json.dumps({"instructions": instructions})
    fanout = 0
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
            fanout += 1
        except asyncio.QueueFull:  # pragma: no cover
            pass
    log.info("[proactive-agent] trigger fan-out: %d subscriber(s)", fanout)
    if fanout == 0:
        # No browser is subscribed to /proactive-events. The operator's
        # webhook should see a hard failure here — silently swallowing
        # the trigger would hide a real misconfiguration (the browser
        # window is closed, the SSE stream timed out, etc.).
        raise HTTPException(503, "no subscribers connected to /proactive-events")
    return {"ok": True, "subscribers": fanout}


@app.get("/proactive-events")
async def proactive_events() -> StreamingResponse:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
    _subscribers.append(queue)

    async def stream() -> AsyncIterator[bytes]:
        try:
            yield b": connected\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                yield f"event: trigger\ndata: {msg}\n\n".encode("utf-8")
        finally:
            if queue in _subscribers:
                _subscribers.remove(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/proactive.html")
async def proactive_html() -> FileResponse:
    return FileResponse(pathlib.Path(__file__).resolve().parent / "proactive.html")


@app.get("/proactive.js")
async def proactive_js() -> FileResponse:
    return FileResponse(
        pathlib.Path(__file__).resolve().parent / "proactive.js",
        media_type="application/javascript",
    )


@app.get("/demo_script.json")
async def demo_script_json() -> FileResponse:
    return FileResponse(
        pathlib.Path(__file__).resolve().parent / "demo_script.json",
        media_type="application/json",
    )


__all__ = ["app", "proactive_trigger", "proactive_events"]
