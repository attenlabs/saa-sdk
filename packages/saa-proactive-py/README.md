<p align="center">
  <a href="../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# `attenlabs-saa-proactive`

Lifecycle helper for **proactive voice agents** on top of [`attenlabs-saa`](https://pypi.org/project/attenlabs-saa/). The agent speaks first; SAA gates the reply.

Apache-2.0. Python 3.10+. Zero hard runtime deps (FastAPI is optional). Peer-dep on `attenlabs-saa`.

## Install

```bash
pip install attenlabs-saa-proactive attenlabs-saa
# Optional FastAPI router:
pip install 'attenlabs-saa-proactive[fastapi]'
```

## `ProactiveLifecycle`

Wraps a `speak` callback with `mark_responding(True) → speak → tail → mark_responding(False)`. Catches errors and releases the gate either way.

```python
import os
from saa import AttentionClient
from saa_proactive import ProactiveLifecycle

saa = AttentionClient(token=os.environ["ATTENLABS_TOKEN"])
saa.start()

lifecycle = ProactiveLifecycle(client=saa, tail_ms=200)

async def speak():
    # Your framework's "speak first" call. Examples:
    #   Pipecat:  await task.queue_frames([LLMRunFrame()])
    #   LiveKit:  await session.generate_reply(instructions="…")
    ...

await lifecycle.run(speak)
```

The lifecycle is single-use per instance; create a fresh one per turn or `await` the previous one. Sync and async `speak` callbacks are both supported (`inspect.isawaitable` handles the dispatch).

```python
from saa_proactive import run_proactive_turn
await run_proactive_turn(client=saa, speak=agent.speak_opening_line)
```

## `TriggerHub`

In-process pub/sub for proactive-turn events. Used by framework overlays to relay `POST /trigger` HTTP webhooks to connected browsers via Server-Sent Events.

```python
from fastapi import FastAPI
from saa_proactive import TriggerHub

app = FastAPI()
hub = TriggerHub()

# Canonical /trigger + /trigger-events shape in one line:
app.include_router(hub.fastapi_router())

async def dispatch():
    sub = hub.subscribe()
    async for event in sub.events():
        await agent.handle_proactive(event.instructions)
```

The `fastapi_router()` helper is opt-in. The hub also exposes:

- `publish(instructions, **extra)`: validates `instructions` is a non-empty string; returns the subscriber count.
- `subscribe()`: returns a subscriber with `events()` (async iterator of `TriggerEvent`) and `sse_lines()` (async iterator of pre-formatted SSE bytes).

## What this package does not do

- **It does not decide *when* to speak.** Proactivity policy lives in your LLM / scheduler / orchestrator.
- **It is not a learned model.** No accuracy claims. The cloud classifier holds those.
- **It does not extend the SAA wire.** No new message types, no new SDK events.

## Tests

```bash
python -m pytest -q packages/saa-proactive-py/tests
```

Includes both pure unit tests and integration tests that instantiate a real `AttentionClient`, intercept `_send_control`, and assert the `mark_responding` flow.

## See also

- [`examples/proactive-agent/`](../../examples/proactive-agent/): framework overlays.
- [`attenlabs-saa`](../saa-py/): the peer-dep cloud SDK.
- [`@attenlabs/saa-gate`](../saa-gate/): the production routing policy state machine.
- [`@attenlabs/saa-proactive`](../saa-proactive-js/): the JS/TS twin.

## License

Apache-2.0. See [`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE).


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
