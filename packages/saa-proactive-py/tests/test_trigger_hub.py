"""Offline tests for TriggerHub fan-out + SSE shape."""
from __future__ import annotations

import asyncio
import json
from typing import List

import pytest

from saa_proactive import TriggerEvent, TriggerHub


@pytest.mark.asyncio
async def test_publish_rejects_malformed_event() -> None:
    hub = TriggerHub()
    with pytest.raises(ValueError, match="instructions"):
        hub.publish("")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="instructions"):
        hub.publish(42)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_publish_fans_out_to_all_subscribers() -> None:
    hub = TriggerHub()
    a = hub.subscribe()
    b = hub.subscribe()
    assert hub.subscriber_count == 2

    fanout = hub.publish("hello")
    assert fanout == 2

    a_gen = a.events()
    b_gen = b.events()
    ev_a = await asyncio.wait_for(a_gen.__anext__(), timeout=1.0)
    ev_b = await asyncio.wait_for(b_gen.__anext__(), timeout=1.0)
    assert ev_a.instructions == "hello"
    assert ev_b.instructions == "hello"

    a.close()
    b.close()
    assert hub.subscriber_count == 0


@pytest.mark.asyncio
async def test_queued_events_arrive_in_order_after_subscribe() -> None:
    hub = TriggerHub()
    sub = hub.subscribe()
    hub.publish("first")
    hub.publish("second")

    gen = sub.events()
    first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    second = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert first.instructions == "first"
    assert second.instructions == "second"
    sub.close()


@pytest.mark.asyncio
async def test_extra_kwargs_round_trip_through_to_json() -> None:
    hub = TriggerHub()
    sub = hub.subscribe()
    hub.publish("hi", priority="high", source="crm")

    gen = sub.events()
    ev = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    payload = json.loads(ev.to_json())
    assert payload["instructions"] == "hi"
    assert payload["priority"] == "high"
    assert payload["source"] == "crm"
    sub.close()


@pytest.mark.asyncio
async def test_sse_lines_emit_connect_comment_then_trigger_frames() -> None:
    hub = TriggerHub()
    sub = hub.subscribe()
    sse = sub.sse_lines()

    connect = await asyncio.wait_for(sse.__anext__(), timeout=1.0)
    assert connect == b": connected\n\n"

    hub.publish("go")
    frame = await asyncio.wait_for(sse.__anext__(), timeout=1.0)
    assert frame.startswith(b"event: trigger\n")
    assert b'"instructions": "go"' in frame
    sub.close()


def test_trigger_event_to_json_dataclass_shape() -> None:
    ev = TriggerEvent(instructions="hi", extra={"src": "cli"})
    payload = json.loads(ev.to_json())
    assert payload == {"instructions": "hi", "src": "cli"}
