"""Proactive overlay on examples/pipecat/bot.py.

The parent bot already injects an opening turn on ``on_client_connected``
(see ``examples/pipecat/bot.py``, line 169-175). This overlay adds a
**second** trigger surface for the more general proactive case: an HTTP
``POST /trigger`` endpoint that lets a CRM webhook, action-item
detector, scheduler, or any back-end event push a proactive turn into
the running Pipecat pipeline mid-call.

The mechanism:

1. ``run_proactive_bot`` runs the parent pipeline AND a small FastAPI
   sidecar listening on ``PROACTIVE_HTTP_PORT``.
2. ``POST /trigger`` body carries ``{"instructions": "..."}`` and is
   pushed onto an asyncio queue.
3. A background task drains the queue, asserts SAA's
   ``mark_responding(True)`` (via the SAA gate's session control), and
   queues an ``LLMRunFrame`` with the proactive instructions into the
   live ``PipelineTask``.
4. The parent pipeline's TTS runs, the gate suppresses SAA during the
   agent's turn, and the callee's reply (when it arrives) is gated as
   usual.

No SDK changes; no SAA wire extension. The proactive opening turn is a
property of the orchestrator (the back-end that posts to ``/trigger``);
SAA stays a pure gating primitive.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import sys
from typing import Optional


# Put the sibling pipecat adapter on sys.path so we can reuse run_bot.
_PARENT = pathlib.Path(__file__).resolve().parent.parent.parent / "pipecat"
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))


log = logging.getLogger("saa.proactive_agent.pipecat")


# Process-level proactive queue. Populated by POST /trigger, drained by
# the pipeline-side ``_proactive_dispatch`` task.
_proactive_queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=64)


def load_demo_script() -> dict:
    """Load the campaign script from ``demo_script.json``."""
    path = pathlib.Path(__file__).resolve().parent / "demo_script.json"
    if not path.is_file():
        return {
            "opening_line": "Hi, I have an update on your build.",
            "system_prompt": "You are a helpful proactive assistant.",
        }
    return json.loads(path.read_text(encoding="utf-8"))


async def _proactive_dispatch(task, context, saa_gate) -> None:
    """Drain the proactive queue and inject LLMRunFrames into the pipeline.

    Each item is a string of instructions (the agent's opening line for
    this proactive turn). We:

    * push a developer-role message into the LLM context so the model
      has the instructions for this turn,
    * queue an ``LLMRunFrame`` so the model runs and TTS produces the
      opening audio,
    * (the SAA gate's ``suppress_during_bot_speech`` handles the
      ``mark_responding`` lifecycle automatically when the TTS output
      reaches the pipeline boundary).
    """
    from pipecat.frames.frames import LLMRunFrame  # noqa: E402

    while True:
        instructions = await _proactive_queue.get()
        log.info("[proactive-agent] dispatching: %s", instructions[:80])
        context.add_message({"role": "developer", "content": instructions})
        await task.queue_frames([LLMRunFrame()])


def _start_http_sidecar(port: int) -> "asyncio.Task[None]":
    """Spin up a small FastAPI server on a background thread.

    Kept dependency-light: only ``fastapi`` + ``uvicorn``, both already
    in the parent's ``requirements.txt`` for the overlay server.
    """
    try:
        import uvicorn
        from fastapi import Body, FastAPI, HTTPException
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "fastapi+uvicorn required for proactive sidecar: "
            "pip install -r requirements.txt"
        ) from exc

    app = FastAPI(title="saa-pipecat-proactive-trigger")

    @app.post("/trigger")
    async def trigger(payload: dict = Body(...)) -> dict:
        instructions = payload.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise HTTPException(400, "missing 'instructions' (string)")
        try:
            _proactive_queue.put_nowait(instructions)
        except asyncio.QueueFull:  # pragma: no cover
            raise HTTPException(503, "proactive queue saturated, try again later")
        return {"ok": True, "queued": _proactive_queue.qsize()}

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "queued": _proactive_queue.qsize()}

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    return asyncio.create_task(server.serve(), name="proactive-sidecar")


async def run_proactive_bot(transport, runner_args, *, upstream_mode: bool = True) -> None:
    """Run the parent pipecat bot with a proactive HTTP sidecar attached.

    Mirrors ``examples/pipecat/bot.py:run_bot`` but plugs in the
    proactive dispatch task and HTTP sidecar so back-end events can
    drive agent-initiated turns.
    """
    # Reuse the parent's _build_gate and the rest of the wiring. We
    # duplicate enough of run_bot's body to expose ``task``, ``context``,
    # and ``saa`` to the dispatcher, but the heavy lifting lives in the
    # parent module.
    from bot import _build_gate, _require_env, SYSTEM_PROMPT  # noqa: E402
    from pipecat.frames.frames import LLMRunFrame  # noqa: E402, F401
    from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
    from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
    from pipecat.pipeline.task import PipelineParams, PipelineTask  # noqa: E402
    from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
    from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.services.cartesia.tts import CartesiaTTSService  # noqa: E402
    from pipecat.services.deepgram.stt import DeepgramSTTService  # noqa: E402
    from pipecat.services.openai.llm import OpenAILLMService  # noqa: E402
    from saa_gate import SAA_SAMPLE_RATE  # noqa: E402

    _require_env("ATTENLABS_TOKEN", "DEEPGRAM_API_KEY", "OPENAI_API_KEY", "CARTESIA_API_KEY")

    script = load_demo_script()

    saa = _build_gate(upstream_mode=upstream_mode)
    stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])
    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        settings=CartesiaTTSService.Settings(
            voice=os.environ.get("CARTESIA_VOICE_ID", script.get("voice", "71a7ad14-091c-4e8e-a314-022ece01c121")),
        ),
    )
    llm = OpenAILLMService(
        api_key=os.environ["OPENAI_API_KEY"],
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        settings=OpenAILLMService.Settings(
            system_instruction=script.get("system_prompt", SYSTEM_PROMPT),
        ),
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=None),
    )
    pipeline = Pipeline([
        transport.input(), saa, stt, user_aggregator, llm, tts,
        transport.output(), assistant_aggregator,
    ])
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=SAA_SAMPLE_RATE,
        ),
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )

    # Proactive opening turn on client connect (preserves parent
    # behaviour) + proactive dispatch task draining the HTTP queue.
    @transport.event_handler("on_client_connected")
    async def _on_connected(transport, client):  # noqa: ARG001
        log.info("client connected, proactive opening turn")
        context.add_message({
            "role": "developer",
            "content": script.get("opening_line", "Briefly introduce yourself."),
        })
        await task.queue_frames([LLMRunFrame()])

    dispatcher = asyncio.create_task(
        _proactive_dispatch(task, context, saa),
        name="proactive-dispatch",
    )
    port = int(os.environ.get("PROACTIVE_HTTP_PORT", "8765"))
    sidecar = _start_http_sidecar(port)
    log.info("[proactive-agent] HTTP sidecar listening on :%d/trigger", port)

    try:
        runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
        await runner.run(task)
    finally:
        dispatcher.cancel()
        sidecar.cancel()


async def bot(runner_args) -> None:
    """Pipecat-runner entrypoint (proactive variant)."""
    from bot import transport_params  # noqa: E402
    from pipecat.runner.utils import create_transport  # noqa: E402
    transport = await create_transport(runner_args, transport_params)
    await run_proactive_bot(transport, runner_args, upstream_mode=True)
