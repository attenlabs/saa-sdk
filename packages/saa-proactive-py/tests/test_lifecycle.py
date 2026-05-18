"""Offline tests for ProactiveLifecycle (sync + async clients)."""
from __future__ import annotations

import asyncio
import time
from typing import List

import pytest

from saa_proactive import ProactiveLifecycle, run_proactive_turn


class _SyncClient:
    def __init__(self) -> None:
        self.calls: List[bool] = []

    def mark_responding(self, active: bool) -> None:
        self.calls.append(active)


class _AsyncClient:
    def __init__(self) -> None:
        self.calls: List[bool] = []

    async def mark_responding(self, active: bool) -> None:
        await asyncio.sleep(0)
        self.calls.append(active)


@pytest.mark.asyncio
async def test_happy_path_sync_client() -> None:
    client = _SyncClient()
    speak_called = False

    async def speak() -> None:
        nonlocal speak_called
        assert client.calls == [True]
        speak_called = True

    await ProactiveLifecycle(client=client, tail_ms=0).run(speak)
    assert speak_called
    assert client.calls == [True, False]


@pytest.mark.asyncio
async def test_happy_path_async_client() -> None:
    client = _AsyncClient()
    await ProactiveLifecycle(client=client, tail_ms=0).run(lambda: None)
    assert client.calls == [True, False]


@pytest.mark.asyncio
async def test_speak_raising_still_releases_the_gate() -> None:
    client = _SyncClient()
    with pytest.raises(RuntimeError, match="speak failed"):
        await ProactiveLifecycle(client=client, tail_ms=0).run(
            lambda: (_ for _ in ()).throw(RuntimeError("speak failed"))
        )
    assert client.calls == [True, False]


@pytest.mark.asyncio
async def test_rejects_concurrent_run_on_same_instance() -> None:
    client = _SyncClient()
    lifecycle = ProactiveLifecycle(client=client, tail_ms=0)

    inner_error: List[str] = []

    async def speak_outer() -> None:
        try:
            await lifecycle.run(lambda: None)
        except RuntimeError as exc:
            inner_error.append(str(exc))

    await lifecycle.run(speak_outer)
    assert len(inner_error) == 1
    assert "already active" in inner_error[0]


@pytest.mark.asyncio
async def test_tail_ms_delays_the_false_assertion() -> None:
    client = _SyncClient()
    start = time.monotonic()
    await ProactiveLifecycle(client=client, tail_ms=50).run(lambda: None)
    elapsed_ms = (time.monotonic() - start) * 1000
    assert elapsed_ms >= 50, f"tail_ms should have delayed >=50ms, got {elapsed_ms:.1f}"


def test_rejects_bad_client() -> None:
    with pytest.raises(TypeError, match="mark_responding"):
        ProactiveLifecycle(client=object())  # type: ignore[arg-type]


def test_rejects_negative_tail_ms() -> None:
    with pytest.raises(ValueError, match="tail_ms"):
        ProactiveLifecycle(client=_SyncClient(), tail_ms=-1)


def test_rejects_non_callable_speak() -> None:
    lifecycle = ProactiveLifecycle(client=_SyncClient(), tail_ms=0)
    with pytest.raises(TypeError, match="speak must be callable"):
        asyncio.get_event_loop().run_until_complete(
            lifecycle.run("not-a-callable")  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_run_proactive_turn_convenience() -> None:
    client = _SyncClient()
    await run_proactive_turn(client=client, tail_ms=0, speak=lambda: None)
    assert client.calls == [True, False]
