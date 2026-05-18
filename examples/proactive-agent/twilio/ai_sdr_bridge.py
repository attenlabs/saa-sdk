"""AI-SDR / proactive-outbound bridge for the SAA x Twilio adapter.

This subclass of ``OpenAIRealtimeBridge`` makes the agent speak first.
That is the mechanical definition of a proactive voice agent: the agent
opens the conversation; the human's first utterance is a *reply*. SAA's
job is to gate that reply against everything else the mic hears
(coworkers, kids, hold music, the other party in a 3-way call).

Why this matters for SAA's story:

* In a reactive voice agent, the human always speaks first. SAA's gate
  fires before any LLM/TTS round-trip.
* In a proactive voice agent (every outbound AI-SDR call, every callback
  agent, every CRM-triggered outreach), the *agent* always speaks first.
  Wake words and PTT both break this flow at the seam. SAA's gating
  primitive is what survives the seam: ``mark_responding(True)`` for the
  agent's opening turn, then ``mark_responding(False)``, then classify
  the human's reply.

This bridge does the minimum delta on top of ``OpenAIRealtimeBridge``:

1. After the Realtime session is configured, it injects a
   ``response.create`` event with the AI-SDR opening line so the model
   begins speaking immediately.
2. The existing adapter (``server.py``) auto-fires
   ``mark_responding(True)`` the instant outbound bytes hit the
   ``outbound_pcm16_16k`` queue, so SAA never re-fires on the agent's
   own opening turn.
3. The existing barge-in handler (``on_user_speech_started``) keeps
   working: if the callee interrupts the opening line, SAA's
   classification fires immediately and the in-flight Realtime response
   is cancelled.

No SDK changes. No new SAA wire message types. The proactivity policy
(when, why, what script) lives in this file; SAA stays a pure gating
primitive.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
from typing import Optional

# The proactive-agent example reuses examples/twilio/'s infrastructure
# (server.py, bridge.py, bridge_openai_realtime.py, audio.py, twiml.py).
# Put the sibling directory on sys.path so we can subclass directly.
_TWILIO = pathlib.Path(__file__).resolve().parent.parent.parent / "twilio"
if str(_TWILIO) not in sys.path:
    sys.path.insert(0, str(_TWILIO))

from bridge import CallContext, CallSession  # noqa: E402
from bridge_openai_realtime import (  # noqa: E402
    DEFAULT_MODEL,
    DEFAULT_VOICE,
    OpenAIRealtimeBridge,
)


log = logging.getLogger("saa.proactive_agent.ai_sdr")


DEFAULT_OPENING_LINE = (
    "Hi, this is Iris calling from Attention Labs. I'm an AI assistant "
    "following up on your warranty enquiry from last week. Do you have "
    "thirty seconds for a quick question?"
)

DEFAULT_SYSTEM_PROMPT = (
    "You are Iris, a friendly AI sales-development representative on an "
    "outbound phone call. Keep replies under two sentences. If the callee "
    "is busy or uninterested, thank them politely and end the call. If "
    "the callee asks to be removed from the list, confirm and end the "
    "call. Never claim to be human; if asked, say you are an AI assistant."
)


def load_demo_script(path: Optional[pathlib.Path] = None) -> dict:
    """Load the AI-SDR demo script from ``demo_script.json``.

    The script is a small JSON file the operator edits per campaign. It
    is *not* a wire schema; keep the keys stable but feel free to add
    fields for your own bridges.
    """
    if path is None:
        path = pathlib.Path(__file__).resolve().parent / "demo_script.json"
    if not path.is_file():
        return {
            "opening_line": DEFAULT_OPENING_LINE,
            "system_prompt": DEFAULT_SYSTEM_PROMPT,
        }
    return json.loads(path.read_text(encoding="utf-8"))


class AISDRBridge(OpenAIRealtimeBridge):
    """OpenAI Realtime bridge that speaks first.

    The proactive turn is a single ``response.create`` event emitted
    after ``session.update``. Realtime synthesises the opening line as
    audio; the adapter pipes it through ``outbound_pcm16_16k``, which
    auto-asserts ``mark_responding(True)`` on the SAA cloud so the
    classifier suppresses predictions during the agent's turn.
    """

    def __init__(
        self,
        *,
        opening_line: str = DEFAULT_OPENING_LINE,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        **kwargs,
    ) -> None:
        super().__init__(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            model=model,
            voice=voice,
            system_prompt=system_prompt,
            **kwargs,
        )
        self._opening_line = opening_line

    @classmethod
    def from_env(cls) -> "AISDRBridge":
        """Construct from environment + ``demo_script.json``.

        Convenience constructor for ``set_bridge_factory(AISDRBridge.from_env)``
        in ``main.py``. Reads the campaign script from
        ``demo_script.json`` so operators can edit the opening line and
        system prompt without redeploying.
        """
        script = load_demo_script()
        return cls(
            opening_line=script.get("opening_line", DEFAULT_OPENING_LINE),
            system_prompt=script.get("system_prompt", DEFAULT_SYSTEM_PROMPT),
            voice=script.get("voice", DEFAULT_VOICE),
            model=script.get("model", DEFAULT_MODEL),
        )

    async def open(self, ctx: CallContext, session: CallSession) -> None:
        await super().open(ctx, session)
        # Proactive opening turn. The Realtime session is configured;
        # asking for a response with explicit instructions makes the
        # model speak first without waiting for caller audio.
        #
        # mark_responding(True) is asserted by the adapter the instant
        # outbound bytes hit the outbound_pcm16_16k queue (see
        # examples/twilio/server.py). We ALSO assert it here so the
        # "thinking" window between response.create and the first audio
        # byte is covered — otherwise a stray sound classified as
        # device-directed during that gap would dispatch on_speech and
        # cancel the proactive opening before it starts. This mirrors
        # the manual assertion the parent class does in on_speech().
        log.info(
            "[ai-sdr] proactive open: call=%s direction=%s "
            "opening_line=%r",
            ctx.call_sid, ctx.direction, self._opening_line[:60],
        )
        await self._send_json({
            "type": "response.create",
            "response": {
                "modalities": ["audio", "text"],
                "instructions": self._opening_line,
            },
        })
        if session is not None:
            await session.mark_responding(True)
