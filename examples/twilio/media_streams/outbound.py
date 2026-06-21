"""Outbound dialer CLI for the SAA × Twilio adapter.

Uses the Twilio REST API to place an outbound call. Twilio dials the
called party, then fetches your ``/voice/outbound`` TwiML and connects
the resulting Media Stream to your running ``server.py``.

Usage::

    export TWILIO_ACCOUNT_SID=AC...
    export TWILIO_AUTH_TOKEN=...
    export TWILIO_FROM_NUMBER=+15551234567
    export PUBLIC_HOSTNAME=your-host.example.com
    python -m outbound +15551112222

The call connects to the same ``/twilio`` WebSocket as inbound calls. Your
:class:`Bridge` answers on the first :meth:`Bridge.open` callback.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.stderr.write(f"error: {name} not set\n")
        sys.exit(2)
    return value


def place_call(
    to_number: str,
    *,
    from_number: Optional[str] = None,
    public_hostname: Optional[str] = None,
    record: bool = False,
) -> str:
    """Place an outbound call. Returns the Twilio Call SID."""
    try:
        from twilio.rest import Client
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "twilio is required: pip install -r examples/twilio/requirements.txt"
        ) from exc

    account = _require_env("TWILIO_ACCOUNT_SID")
    auth = _require_env("TWILIO_AUTH_TOKEN")
    from_number = from_number or _require_env("TWILIO_FROM_NUMBER")
    public_hostname = public_hostname or _require_env("PUBLIC_HOSTNAME")

    twiml_url = f"https://{public_hostname}/voice/outbound"
    status_callback = f"https://{public_hostname}/twilio-status"

    client = Client(account, auth)
    call = client.calls.create(
        to=to_number,
        from_=from_number,
        url=twiml_url,
        method="POST",
        record=record,
        status_callback=status_callback,
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        status_callback_method="POST",
    )
    return call.sid


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Place an outbound call via the SAA × Twilio adapter.")
    parser.add_argument("to", help="E.164 destination number, e.g. +15551112222")
    parser.add_argument("--from", dest="from_number", default=None, help="Caller-ID Twilio number (defaults to TWILIO_FROM_NUMBER).")
    parser.add_argument("--public-hostname", default=None, help="Externally-visible hostname (defaults to PUBLIC_HOSTNAME).")
    parser.add_argument("--record", action="store_true", help="Enable Twilio call recording.")
    args = parser.parse_args(argv)

    sid = place_call(
        args.to,
        from_number=args.from_number,
        public_hostname=args.public_hostname,
        record=args.record,
    )
    print(f"Call placed: {sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
