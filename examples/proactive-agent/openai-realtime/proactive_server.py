"""Proactive overlay on examples/openai-realtime/server.py.

Reuses the parent relay's ``app`` (mints OpenAI ``client_secret``, serves
``/twilio`` WebSocket bridge, serves static files) and adds two
endpoints that turn the example proactive:

* ``POST /proactive-trigger`` - a CRM / scheduler / notification
  back-end calls this with ``{"instructions": "..."}`` and the trigger
  is fan-out to every connected browser via Server-Sent Events.
* ``GET /proactive-events`` - Server-Sent Events stream the browser
  subscribes to. When a trigger arrives it dispatches an ``event:
  trigger`` SSE; the browser side (``proactive.js``) consumes it,
  calls ``markResponding(true)``, and sends ``response.create`` to
  OpenAI Realtime.

Run with::

    uvicorn proactive_server:app --host 0.0.0.0 --port 8000 --reload

The proactive HTML is served at ``GET /proactive.html``; the parent
example's reactive ``index.html`` is still at ``GET /``.

No SDK changes; no SAA wire extension. ``mark_responding(True/False)`` is
asserted by the browser-side ``proactive.js`` because in browser-direct
mode the relay does NOT hold the Realtime WebSocket; the browser does.
For the Twilio bridge (the relay HOLDS the Realtime WS), see
``examples/proactive-agent/twilio/`` instead.
"""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import sys
from typing import AsyncIterator


# Put the sibling openai-realtime adapter on sys.path BEFORE importing it
# so the adapter's relative imports resolve.
_PARENT = pathlib.Path(__file__).resolve().parent.parent.parent / "openai-realtime"
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

# Reuse the parent's FastAPI app (mints ephemeral OpenAI tokens, serves
# static files, etc.). The overlay only adds proactive endpoints.
from server import app  # noqa: E402

try:
    from fastapi import Body, HTTPException
    from fastapi.responses import FileResponse, StreamingResponse
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "fastapi is required: pip install -r requirements.txt"
    ) from exc


log = logging.getLogger("saa.proactive_agent.openai_realtime")


# In-process pub/sub for proactive triggers. One asyncio.Queue per
# connected SSE client; POST /proactive-trigger fan-outs to all of them.
_subscribers: list[asyncio.Queue[str]] = []


@app.post("/proactive-trigger")
async def proactive_trigger(payload: dict = Body(...)) -> dict:
    """Fire a proactive turn at every connected browser.

    Body shape::

        {"instructions": "Your tests are red - want me to look?"}

    Wire this to your CRM webhook, scheduler, build-broke notification,
    or any back-end event. The browser-side ``proactive.js`` asserts
    ``markResponding(true)`` before sending ``response.create`` to
    OpenAI Realtime so SAA suppresses predictions through the agent's
    opening turn.
    """
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
    """Server-Sent Events stream for proactive triggers.

    Each connected browser opens an EventSource on this endpoint;
    proactive triggers arrive as ``event: trigger`` with a JSON data
    payload. The connection stays open for the life of the browser tab.
    """
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
    _subscribers.append(queue)

    async def stream() -> AsyncIterator[bytes]:
        try:
            # Send a comment line on connect so proxies don't time out.
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
    """Serve the proactive variant's HTML entry point."""
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
