"""Proactive AI-SDR overlay on the SAA x Twilio adapter.

This module reuses ``examples/twilio/`` for all of the heavy lifting
(Twilio Media Streams handling, SAA gating, mu-law/PCM16 codec, signature
validation, paced outbound playback, barge-in, mark_responding wiring)
and adds two small things on top:

* ``AISDRBridge`` is registered as the per-call bridge factory, so every
  call (inbound or outbound) opens with a proactive turn synthesised by
  OpenAI Realtime.
* ``POST /place-proactive-call`` exposes an HTTP webhook that places an
  outbound AI-SDR call from a CRM / scheduling / notification trigger.
  The call itself is placed via ``examples/twilio/outbound.py``.

Run with::

    uvicorn main:app --host 0.0.0.0 --port 8000

See ``README.md`` for the full runbook and ``examples/twilio/README.md``
for the underlying adapter's reference docs.
"""
from __future__ import annotations

import logging
import os
import pathlib
import sys
from typing import Optional

# Put the sibling Twilio adapter on sys.path BEFORE importing it so the
# adapter's relative imports (``from bridge import ...``, etc.) resolve.
_TWILIO = pathlib.Path(__file__).resolve().parent.parent.parent / "twilio"
if str(_TWILIO) not in sys.path:
    sys.path.insert(0, str(_TWILIO))

# These imports MUST stay after the sys.path mutation. They re-export
# the FastAPI ``app`` from the Twilio adapter and the bridge factory
# hook documented in examples/twilio/server.py.
from server import app, set_bridge_factory  # noqa: E402

from ai_sdr_bridge import AISDRBridge  # noqa: E402

try:
    from fastapi import Body, HTTPException
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "fastapi is required: pip install -r examples/proactive-agent/requirements.txt"
    ) from exc


log = logging.getLogger("saa.proactive_agent")


# Register the proactive bridge as the default for every call this
# server handles. Inbound calls still work, the agent simply opens with
# the AI-SDR script instead of waiting for the caller to speak.
set_bridge_factory(AISDRBridge.from_env)


@app.post("/place-proactive-call")
async def place_proactive_call(payload: dict = Body(...)) -> JSONResponse:
    """Place an outbound proactive call.

    Body shape (all fields optional except ``to``)::

        {
            "to": "+15551112222",            # E.164 destination, required
            "from_number": "+15550001111",   # caller-ID, defaults to TWILIO_FROM_NUMBER
            "public_hostname": "...",        # defaults to PUBLIC_HOSTNAME
            "record": false                  # Twilio call recording
        }

    On success the response carries the Twilio Call SID. Wire this
    endpoint to a CRM / scheduling / notification trigger to make your
    agent proactive: a webhook arrives, the agent dials the customer,
    speaks the opening line, and SAA gates everything the carrier hears
    until the customer addresses the phone.
    """
    to_number = payload.get("to")
    if not isinstance(to_number, str) or not to_number.strip():
        raise HTTPException(400, "missing 'to' (E.164 destination number)")

    # Defer the import so unit tests that don't have ``twilio`` installed
    # can still import this module.
    from outbound import place_call  # noqa: E402

    try:
        sid = place_call(
            to_number,
            from_number=payload.get("from_number"),
            public_hostname=payload.get("public_hostname"),
            record=bool(payload.get("record", False)),
        )
    except SystemExit as exc:  # missing env var or twilio install
        raise HTTPException(500, str(exc) or "outbound dialer failed")

    log.info("[proactive-agent] placed call: sid=%s to=%s", sid, to_number)
    return JSONResponse({"call_sid": sid, "to": to_number})


__all__ = ["app", "set_bridge_factory", "place_proactive_call"]
