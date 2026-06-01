"""build_attention_runner — greenfield helper.

Composes `attention_agent_token` + `start_attention_session` + `AttentionEngine`
into a single async `run(room_url, room_name, human_identity, transport, task)`
function ready to drop into a Pipecat bot. Consumers writing their FIRST
Pipecat + Daily voice agent don't need to know about the underlying
primitives — they hand us a `handle_turn(event, transport)` callback and
we wire everything else.

Note: this factory does NOT construct `DailyTransport` or `PipelineTask`
for the consumer. Both are pipeline-shape-dependent (services, frame
order, observers, …) and always belong to the consumer's code. Pass them
into the returned `run(...)` call.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from pipecat.transports.daily.transport import DailyTransport

from .api import start_attention_session
from .engine import AttentionEngine
from .tokens import DEFAULT_AGENT_IDENTITY, attention_agent_token
from .types import InterjectionEvent, InterruptEvent, TurnReadyEvent


logger = logging.getLogger("saa_pipecat_client.factory")


OnTurnCallback = Callable[[TurnReadyEvent, DailyTransport], Awaitable[None]]
OnInterruptCallback = Callable[[InterruptEvent, DailyTransport], Awaitable[None]]
OnInterjectionCallback = Callable[[InterjectionEvent, DailyTransport], Awaitable[None]]

RunCallable = Callable[[str, str, str, DailyTransport, Any], Awaitable[AttentionEngine]]


def build_attention_runner(
    *,
    on_turn: OnTurnCallback,
    on_interrupt: OnInterruptCallback | None = None,
    on_interjection: OnInterjectionCallback | None = None,
    daily_api_key: str | None = None,
    saa_api_key: str | None = None,
    attention_config: dict[str, Any] | None = None,
    agent_identity: str = DEFAULT_AGENT_IDENTITY,
    api_base: str | None = None,
) -> RunCallable:
    """Build an async `run(room_url, room_name, human_identity, transport, task)`
    function that:

      1. Mints a hidden-bot Daily meeting token via `attention_agent_token`.
      2. POSTs `/v1/sessions/daily` to summon the hosted attention bot into
         the customer's room.
      3. Constructs an `AttentionEngine` over the supplied `transport`,
         binds the supplied `task`, and registers the user-supplied
         `on_turn` / `on_interrupt` / `on_interjection` callbacks.
      4. Awaits the engine's `started` handshake, then returns the engine
         (and a shutdown coroutine) to the caller.

    The returned coroutine completes when the engine is ready — the caller
    is expected to run their `PipelineRunner` afterwards and call
    `engine.stop()` / `session.stop()` (or rely on the runner teardown
    callbacks they wire up).
    """
    daily_api_key = daily_api_key or os.getenv("DAILY_API_KEY") or ""
    saa_api_key = saa_api_key or os.getenv("SAA_API_KEY") or ""

    async def run(
        room_url: str,
        room_name: str,
        human_identity: str,
        transport: DailyTransport,
        task: Any,
    ) -> AttentionEngine:
        missing = [
            name for name, val in [
                ("SAA_API_KEY", saa_api_key),
                ("DAILY_API_KEY", daily_api_key),
            ] if not val
        ]
        if missing:
            raise RuntimeError(
                f"build_attention_runner: missing required value(s): "
                f"{', '.join(missing)}. Pass via kwargs or set the env var(s)."
            )

        agent_token = attention_agent_token(
            daily_api_key=daily_api_key,
            room_name=room_name,
            identity=agent_identity,
        )

        session = await start_attention_session(
            api_key=saa_api_key,
            room_url=room_url,
            agent_token=agent_token,
            participant_identity=human_identity,
            attention_config=attention_config,
            **({"api_base": api_base} if api_base else {}),
        )
        logger.info(
            "attention session %s started — wiring engine", session.session_id,
        )

        engine = AttentionEngine(
            transport,
            agent_identity=session.agent_identity,
            task=task,
        )

        if on_interrupt is not None:
            @engine.on_interrupt
            async def _on_interrupt(ev: InterruptEvent) -> None:
                await on_interrupt(ev, transport)

        if on_interjection is not None:
            @engine.on_interjection
            async def _on_interjection(ev: InterjectionEvent) -> None:
                await on_interjection(ev, transport)

        @engine.on_turn_ready
        async def _on_turn(ev: TurnReadyEvent) -> None:
            await on_turn(ev, transport)

        # Register shutdown hooks on the PipelineTask if it supports them.
        # Pipecat's PipelineTask exposes `add_observer` and similar; the
        # generic shutdown surface is to call engine.stop() / session.stop()
        # from the caller's `finally:` block. We expose both handles on the
        # engine for that purpose.
        engine._attached_session = session  # type: ignore[attr-defined]

        await engine.start()
        return engine

    return run
