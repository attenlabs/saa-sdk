"""End-to-end contract test: real ``attenlabs-saa`` AttentionClient +
real ``attenlabs-saa-proactive`` ProactiveLifecycle. Verifies the
``mark_responding(True)`` → speak → ``mark_responding(False)`` sequence
flows through the AttentionClient's actual public method, with the
SDK's internal send captured.

This test does not open a network connection; it intercepts the
SDK's ``_send_control`` to observe the wire-level intent.
"""
from __future__ import annotations

import asyncio

import pytest

from saa import AttentionClient
from saa_proactive import ProactiveLifecycle


def _capture_sends(client: AttentionClient) -> list[dict]:
    sends: list[dict] = []

    def _send_control(msg: dict) -> None:
        sends.append(msg)

    # The SDK's send is internal-by-convention but addressable at
    # runtime. Replacing it lets us verify the wire-level intent of
    # mark_responding without opening a real WebSocket.
    client._send_control = _send_control  # type: ignore[assignment]
    return sends


@pytest.mark.asyncio
async def test_lifecycle_wraps_speak_with_mark_responding() -> None:
    client = AttentionClient(token="test-token-not-sent")
    sends = _capture_sends(client)

    lifecycle = ProactiveLifecycle(client=client, tail_ms=10)
    spoke = False

    async def speak() -> None:
        nonlocal spoke
        spoke = True

    await lifecycle.run(speak)

    assert spoke is True
    assert [s["action"] for s in sends] == ["responding_start", "responding_stop"]


@pytest.mark.asyncio
async def test_lifecycle_releases_gate_when_speak_raises() -> None:
    client = AttentionClient(token="test-token-not-sent")
    sends = _capture_sends(client)
    lifecycle = ProactiveLifecycle(client=client, tail_ms=10)

    async def speak() -> None:
        raise RuntimeError("tts failed")

    with pytest.raises(RuntimeError, match="tts failed"):
        await lifecycle.run(speak)

    assert [s["action"] for s in sends] == ["responding_start", "responding_stop"]


@pytest.mark.asyncio
async def test_lifecycle_is_single_use_per_instance() -> None:
    client = AttentionClient(token="test-token-not-sent")
    _capture_sends(client)
    lifecycle = ProactiveLifecycle(client=client, tail_ms=10)

    async def slow_speak() -> None:
        await asyncio.sleep(0.05)

    # Start a run but don't await it; second call must reject.
    task = asyncio.create_task(lifecycle.run(slow_speak))
    await asyncio.sleep(0)  # let the first run reach the "active" state

    with pytest.raises(RuntimeError, match="lifecycle already active"):
        await lifecycle.run(slow_speak)

    await task
