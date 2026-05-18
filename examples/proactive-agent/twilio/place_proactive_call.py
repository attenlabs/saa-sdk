"""CLI dialer for the proactive AI-SDR demo.

Thin wrapper around ``examples/twilio/outbound.py`` that places an
outbound call to a single E.164 number. The receiving end (your
running ``main.py`` server, with ``AISDRBridge`` registered) speaks the
opening line from ``demo_script.json`` immediately on call-connect.

Usage::

    export TWILIO_ACCOUNT_SID=AC...
    export TWILIO_AUTH_TOKEN=...
    export TWILIO_FROM_NUMBER=+15550001111
    export PUBLIC_HOSTNAME=your-host.example.com
    python place_proactive_call.py +15551112222

This is the simplest demonstration of *proactive voice*: the agent
dials, speaks first, and SAA gates the callee's reply against
coworkers, kids, hold music, and the other party in a 3-way call.

For HTTP-triggered campaigns, ``POST /place-proactive-call`` against
the running server instead.
"""
from __future__ import annotations

import pathlib
import sys


# Reuse the Twilio adapter's outbound dialer verbatim. No duplication;
# only the proactive *intent* lives in this directory.
_TWILIO = pathlib.Path(__file__).resolve().parent.parent.parent / "twilio"
if str(_TWILIO) not in sys.path:
    sys.path.insert(0, str(_TWILIO))

from outbound import main as _outbound_main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(_outbound_main())
