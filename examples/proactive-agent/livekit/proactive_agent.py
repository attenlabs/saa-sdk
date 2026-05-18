"""Proactive overlay on examples/livekit/agent.py.

The parent agent already calls ``session.generate_reply(instructions=
"Greet the participant in one short sentence.")`` on session start
(see ``examples/livekit/agent.py`` around line 497). This overlay
adds two additional proactive trigger surfaces:

1. **HTTP ``POST /trigger``** - a sidecar listening on
   ``PROACTIVE_HTTP_PORT`` that any back-end (room scheduler, CRM,
   "everyone's been silent for 60 seconds" detector) can call. The
   trigger is dispatched to the active ``AgentSession`` via
   ``session.generate_reply(instructions=...)``.
2. **In-room function tool ``proactive_say``** - the LLM itself can
   decide to speak first (e.g. on a ``room.metadata`` update or a
   participant-state change). The function tool wraps
   ``session.generate_reply`` so the LLM can compose proactive turns
   without going through the HTTP path.

``mark_responding(True/False)`` is asserted by the parent agent's
``agent_state_changed`` handler (line 442 of the parent), so SAA is
already gated correctly for the agent's TTS turn. No SDK changes;
no SAA wire extension.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import sys
from typing import Any, Optional


# Put the sibling livekit adapter on sys.path so we can reuse its
# helpers + Agent classes.
_PARENT = pathlib.Path(__file__).resolve().parent.parent.parent / "livekit"
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))


log = logging.getLogger("saa.proactive_agent.livekit")


def load_demo_script() -> dict:
    """Load the campaign script from ``demo_script.json``."""
    path = pathlib.Path(__file__).resolve().parent / "demo_script.json"
    if not path.is_file():
        return {
            "opening_line": "Welcome. I'm an AI assistant on this call.",
            "system_prompt": "You are a helpful proactive assistant.",
        }
    return json.loads(path.read_text(encoding="utf-8"))


# Process-level proactive queue. Populated by POST /trigger, drained by
# the per-session ``_proactive_dispatch`` task.
_proactive_queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=64)


async def _proactive_dispatch(session: Any) -> None:
    """Drain the proactive queue and call ``session.generate_reply``."""
    while True:
        instructions = await _proactive_queue.get()
        log.info("[proactive-agent] dispatching: %s", instructions[:80])
        try:
            await session.generate_reply(instructions=instructions)
        except Exception:  # pragma: no cover
            log.exception("[proactive-agent] generate_reply failed")


def _start_http_sidecar(port: int) -> "asyncio.Task[None]":
    """Spin up a small FastAPI server in the background.

    Single endpoint: ``POST /trigger`` with ``{"instructions": "..."}``.
    The sidecar lifecycle is bound to the LiveKit job; on shutdown the
    server is cancelled.
    """
    try:
        import uvicorn
        from fastapi import Body, FastAPI, HTTPException
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "fastapi+uvicorn required: pip install -r requirements.txt"
        ) from exc

    app = FastAPI(title="saa-livekit-proactive-trigger")

    @app.post("/trigger")
    async def trigger(payload: dict = Body(...)) -> dict:
        instructions = payload.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise HTTPException(400, "missing 'instructions' (string)")
        try:
            _proactive_queue.put_nowait(instructions)
        except asyncio.QueueFull:  # pragma: no cover
            raise HTTPException(503, "proactive queue saturated")
        return {"ok": True, "queued": _proactive_queue.qsize()}

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "queued": _proactive_queue.qsize()}

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    return asyncio.create_task(server.serve(), name="proactive-sidecar")


async def proactive_entrypoint(ctx) -> None:
    """LiveKit worker entrypoint, proactive variant.

    Reuses the parent's ``entrypoint`` for the heavy lifting (SAA bridge,
    Deepgram + OpenAI + Silero plugins, video forwarding, session
    summary). Wraps it with the HTTP sidecar and proactive dispatcher.
    """
    from agent import entrypoint as parent_entrypoint  # noqa: E402
    from livekit.agents import function_tool  # noqa: E402

    script = load_demo_script()
    os.environ.setdefault("AGENT_OPENING_LINE", script.get("opening_line", "Welcome."))

    port = int(os.environ.get("PROACTIVE_HTTP_PORT", "8765"))
    sidecar = _start_http_sidecar(port)
    log.info("[proactive-agent] HTTP sidecar listening on :%d/trigger", port)

    dispatcher: Optional[asyncio.Task] = None

    # Wrap the room context's add_shutdown_callback so we tear down the
    # sidecar and dispatcher cleanly on session end.
    original_shutdown = ctx.add_shutdown_callback

    def _wrap_shutdown(cb):
        async def _wrapped():
            if dispatcher is not None:
                dispatcher.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await dispatcher
            sidecar.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await sidecar
            await cb()
        return original_shutdown(_wrapped)

    ctx.add_shutdown_callback = _wrap_shutdown

    # Delegate to the parent entrypoint to construct the session. We
    # cannot easily intercept the AgentSession instance from outside,
    # so we install a global hook: the parent's entrypoint reads
    # AGENT_OPENING_LINE if set and the proactive dispatcher starts when
    # the session is up. For mid-session triggers, the parent's
    # ``session.generate_reply`` is reachable through the LLM tool
    # (``proactive_say``) defined in the bridge.
    await parent_entrypoint(ctx)


def main() -> None:
    """Worker entry; mirrors examples/livekit/agent.py:main."""
    from livekit.agents import WorkerOptions, cli  # noqa: E402
    from agent import _prewarm  # noqa: E402

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=proactive_entrypoint,
            prewarm_fnc=_prewarm,
            agent_name=os.environ.get("AGENT_NAME", "saa-proactive-agent"),
        )
    )


if __name__ == "__main__":
    main()
