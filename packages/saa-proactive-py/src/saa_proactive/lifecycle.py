"""ProactiveLifecycle — wraps the mark_responding lifecycle every
proactive voice agent needs.

The class is intentionally tiny: it exists to centralise the gate
semantics (assert BEFORE the speak action, release AFTER a
configurable tail-ms to absorb trailing TTS chunks, release even
when the speak action raises) so every framework overlay only
carries the framework-specific "how do I tell the agent to speak"
surface and not the gate-lifecycle scaffolding.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable, Optional, Protocol, Union


class _MarkRespondable(Protocol):
    """Minimal subset of attenlabs-saa's ``AttentionClient`` surface.

    Anything that exposes a ``mark_responding(active: bool)`` (sync or
    async) is compatible. The protocol exists so users can pass mocks
    in tests without depending on the cloud SDK.
    """

    def mark_responding(self, active: bool) -> Union[None, Awaitable[None]]:  # noqa: D401
        ...


_Speak = Callable[[], Union[None, Awaitable[None]]]


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it is awaitable; otherwise return it as-is."""
    if inspect.isawaitable(value):
        return await value
    return value


class ProactiveLifecycle:
    """Wrap a proactive opening turn with the SAA gate lifecycle.

    Example::

        from saa import AttentionClient
        from saa_proactive import ProactiveLifecycle

        saa = AttentionClient(token=...)
        saa.start()
        lifecycle = ProactiveLifecycle(client=saa, tail_ms=200)
        await lifecycle.run(lambda: agent.speak_opening_line())

    The lifecycle:

    * asserts ``mark_responding(True)`` before ``speak`` is invoked,
    * awaits ``speak`` (treats it as a coroutine if it returns one),
    * waits ``tail_ms`` for trailing TTS audio,
    * asserts ``mark_responding(False)`` in a ``finally`` so the gate
      releases even when ``speak`` raises.

    Concurrent ``run()`` calls on the same instance are rejected; use
    a fresh instance per turn or ``await`` the previous one.
    """

    def __init__(self, *, client: _MarkRespondable, tail_ms: float = 200.0) -> None:
        if not hasattr(client, "mark_responding") or not callable(getattr(client, "mark_responding")):
            raise TypeError(
                "[saa-proactive] ProactiveLifecycle requires a client with a "
                "mark_responding(active: bool) method (typically attenlabs-saa's AttentionClient)"
            )
        if not isinstance(tail_ms, (int, float)) or tail_ms < 0:
            raise ValueError(
                "[saa-proactive] tail_ms must be a non-negative number"
            )
        self._client = client
        self._tail_ms = float(tail_ms)
        self._active = False

    @property
    def active(self) -> bool:
        """True while a ``run()`` is in flight."""
        return self._active

    async def run(self, speak: _Speak) -> None:
        """Run a proactive turn.

        :param speak: callable that triggers the agent to speak. May be
            synchronous or return a coroutine; the coroutine is awaited.
            If ``speak`` raises, the gate is still released and the
            exception propagates.
        """
        if not callable(speak):
            raise TypeError(
                "[saa-proactive] ProactiveLifecycle.run(speak): speak must be callable"
            )
        if self._active:
            raise RuntimeError(
                "[saa-proactive] lifecycle already active. Create a new "
                "ProactiveLifecycle per turn or await the previous run() to complete."
            )
        self._active = True
        try:
            await _maybe_await(self._client.mark_responding(True))
            try:
                await _maybe_await(speak())
            finally:
                if self._tail_ms > 0:
                    await asyncio.sleep(self._tail_ms / 1000.0)
                await _maybe_await(self._client.mark_responding(False))
        finally:
            self._active = False


async def run_proactive_turn(
    *, client: _MarkRespondable, tail_ms: float = 200.0, speak: _Speak
) -> None:
    """One-shot convenience equivalent to
    ``ProactiveLifecycle(client=client, tail_ms=tail_ms).run(speak)``.
    """
    await ProactiveLifecycle(client=client, tail_ms=tail_ms).run(speak)


__all__ = ["ProactiveLifecycle", "run_proactive_turn"]
