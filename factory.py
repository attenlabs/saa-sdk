"""build_attention_entrypoint — greenfield helper.

Composes `attention_agent_token` + `start_attention_session` + `AttentionEngine`
into a single `entrypoint(ctx)` coroutine. Customers writing their FIRST LiveKit
voice agent don't need the underlying primitives — they hand us a
`handle_turn(event, ctx)` callback and we wire everything else.

The returned `entrypoint` is a plain `Callable[[JobContext], Awaitable[None]]`,
so it plugs into either LiveKit Agents shape:

    # AgentServer (current 1.5.x idiom)
    server = AgentServer()
    server.rtc_session()(build_attention_entrypoint(on_turn=handle_turn))
    cli.run_app(server)

    # WorkerOptions (older idiom, still supported on 1.5.x)
    cli.run_app(WorkerOptions(entrypoint_fnc=build_attention_entrypoint(on_turn=handle_turn)))

For an existing voice agent (your own AgentSession), skip this factory and wire
AttentionEngine directly — see the package README and examples/livekit/.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from livekit.agents import JobContext

from .api import start_attention_session
from .engine import AttentionEngine
from .tokens import DEFAULT_AGENT_IDENTITY, attention_agent_token
from .types import InterjectionEvent, InterruptEvent, TurnReadyEvent


logger = logging.getLogger("saa_livekit_client.factory")


OnTurnCallback = Callable[[TurnReadyEvent, JobContext], Awaitable[None]]
OnInterruptCallback = Callable[[InterruptEvent, JobContext], Awaitable[None]]
OnInterjectionCallback = Callable[[InterjectionEvent, JobContext], Awaitable[None]]


def build_attention_entrypoint(
    *,
    on_turn: OnTurnCallback,
    on_interrupt: OnInterruptCallback | None = None,
    on_interjection: OnInterjectionCallback | None = None,
    saa_api_key: str | None = None,
    lk_api_key: str | None = None,
    lk_api_secret: str | None = None,
    livekit_url: str | None = None,
    attention_config: dict[str, Any] | None = None,
    agent_identity: str = DEFAULT_AGENT_IDENTITY,
    api_base: str | None = None,
) -> Callable[[JobContext], Awaitable[None]]:
    """Build an `entrypoint(ctx)` coroutine for `AgentServer.rtc_session()` or `WorkerOptions(entrypoint_fnc=...)`.

    The returned coroutine:
      1. Awaits `ctx.connect()` and the first remote participant.
      2. Issues a hidden-participant agent_token via the customer's LK creds.
      3. POSTs `/v1/sessions/livekit` to summon the hosted attention agent.
      4. Constructs an `AttentionEngine`, wires the supplied callbacks.
      5. Awaits room disconnect, then tears down the hosted session.
    """
    saa_api_key = saa_api_key or os.getenv("SAA_API_KEY") or ""
    lk_api_key = lk_api_key or os.getenv("LIVEKIT_API_KEY") or ""
    lk_api_secret = lk_api_secret or os.getenv("LIVEKIT_API_SECRET") or ""
    livekit_url = livekit_url or os.getenv("LIVEKIT_URL") or ""

    async def entrypoint(ctx: JobContext) -> None:
        # Lazy validation — better DX than raising at module import time
        # if the user is iterating in a REPL.
        missing = [
            name for name, val in [
                ("SAA_API_KEY", saa_api_key),
                ("LIVEKIT_API_KEY", lk_api_key),
                ("LIVEKIT_API_SECRET", lk_api_secret),
                ("LIVEKIT_URL", livekit_url),
            ] if not val
        ]
        if missing:
            raise RuntimeError(
                f"build_attention_entrypoint: missing required value(s): "
                f"{', '.join(missing)}. Pass via kwargs or set the env var(s)."
            )

        await ctx.connect()
        user = await ctx.wait_for_participant()
        logger.info("user joined: identity=%s", user.identity)

        agent_token = attention_agent_token(
            api_key=lk_api_key,
            api_secret=lk_api_secret,
            room_name=ctx.room.name,
            identity=agent_identity,
        )

        session = await start_attention_session(
            api_key=saa_api_key,
            livekit_url=livekit_url,
            agent_token=agent_token,
            room_name=ctx.room.name,
            participant_identity=user.identity,
            attention_config=attention_config,
            **({"api_base": api_base} if api_base else {}),
        )

        engine = AttentionEngine(ctx.room, agent_identity=session.agent_identity)

        if on_interrupt is not None:
            @engine.on_interrupt
            async def _on_interrupt(ev: InterruptEvent) -> None:
                await on_interrupt(ev, ctx)

        if on_interjection is not None:
            @engine.on_interjection
            async def _on_interjection(ev: InterjectionEvent) -> None:
                await on_interjection(ev, ctx)

        @engine.on_turn_ready
        async def _on_turn(ev: TurnReadyEvent) -> None:
            await on_turn(ev, ctx)

        # Defer the hosted-session stop to the JobContext shutdown — handles
        # both normal disconnect and worker SIGTERM paths.
        ctx.add_shutdown_callback(session.stop)
        ctx.add_shutdown_callback(engine.stop)

        await engine.start()

        # Idle until room disconnect — the JobContext returning from this
        # coroutine ends the job from the worker's POV.
        disconnect_evt = asyncio.Event()
        ctx.room.on("disconnected", lambda *_a, **_kw: disconnect_evt.set())
        await disconnect_evt.wait()

    return entrypoint
