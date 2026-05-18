"""TwiML response generators for the Twilio voice webhook.

Twilio's ``<Stream>`` verb opens a bidirectional WebSocket from the carrier
into your server. The TwiML below is the minimum that gives you live media
in both directions; production deployments will typically add ``<Say>``
disclosure (call recording, AI use), DTMF capture, or a ``<Pause>`` for
greeting alignment.

Reference: https://www.twilio.com/docs/voice/twiml/stream
"""
from __future__ import annotations

from typing import Mapping, Optional
from xml.sax.saxutils import escape

_XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>'


def twiml_for_stream(
    stream_url: str,
    *,
    greeting: Optional[str] = None,
    custom_parameters: Optional[Mapping[str, str]] = None,
    bidirectional: bool = True,
) -> str:
    """Return TwiML that opens a Media Stream to ``stream_url``.

    Args:
        stream_url: The ``wss://`` URL of your /twilio WebSocket endpoint.
        greeting: Optional spoken greeting played before the stream opens.
        custom_parameters: Optional ``<Parameter>`` children, Twilio passes
            these back in the ``start`` event's ``customParameters`` field.
            Useful for routing a single endpoint to multiple agents.
        bidirectional: When True (default), Twilio opens a duplex stream
            and accepts outbound ``media`` frames for caller playback.
            Set False for analytics-only deployments.
    """
    parts = [_XML_HEADER, "<Response>"]
    if greeting:
        parts.append(f'<Say voice="alice">{escape(greeting)}</Say>')
    parts.append("<Connect>" if bidirectional else "<Start>")
    parts.append(f'<Stream url="{escape(stream_url, {chr(34): "&quot;"})}">')
    if custom_parameters:
        for name, value in custom_parameters.items():
            parts.append(
                f'<Parameter name="{escape(name, {chr(34): "&quot;"})}" '
                f'value="{escape(value, {chr(34): "&quot;"})}" />'
            )
    parts.append("</Stream>")
    parts.append("</Connect>" if bidirectional else "</Start>")
    parts.append("</Response>")
    return "".join(parts)


def twiml_with_recording_disclosure(
    stream_url: str,
    disclosure: str = (
        "This call may be recorded and analysed by an automated assistant. "
        "Stay on the line to continue, or hang up at any time."
    ),
    **kwargs,
) -> str:
    """TwiML with a spoken disclosure before the stream opens.

    Many jurisdictions require informed consent before AI-mediated audio
    capture. This wraps :func:`twiml_for_stream` with a ``<Say>`` prefix
    that plays before the stream connects.
    """
    return twiml_for_stream(stream_url, greeting=disclosure, **kwargs)


def twiml_reject(reason: str = "busy") -> str:
    """TwiML that rejects the inbound call (e.g. for rate-limit / blocklist)."""
    safe = escape(reason, {chr(34): "&quot;"})
    return f'{_XML_HEADER}<Response><Reject reason="{safe}" /></Response>'


__all__ = [
    "twiml_for_stream",
    "twiml_with_recording_disclosure",
    "twiml_reject",
]
