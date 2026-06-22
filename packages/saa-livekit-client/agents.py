"""Optional livekit-agents glue: drive a RealtimeModel from SAA turns.

Not imported by the package core (which stays transport-only). Import explicitly:
    from saa_livekit_client.agents import inject_realtime_turn
Duck-types the AgentSession/RealtimeSession, so it adds no new dependency.
"""
from __future__ import annotations

from typing import Any

from livekit import rtc

from .types import InterjectionEvent, TurnReadyEvent

_SR = 16000
_STEP = (_SR // 10) * 2  # 100ms of int16 mono, in bytes


def resolve_realtime_session(session: Any) -> Any | None:
    """Live RealtimeSession behind an AgentSession, or None before it starts.

    Reaches a private livekit-agents attribute (no public accessor as of 1.6.x);
    guarded so a layout change degrades to None instead of raising.
    """
    activity = getattr(session, "_activity", None)
    return getattr(activity, "realtime_llm_session", None) if activity is not None else None


def inject_realtime_turn(
    session: Any,
    event: TurnReadyEvent | InterjectionEvent,
    *,
    instructions: str | None = None,
) -> bool:
    """Push an SAA turn/interjection into the realtime model and request a reply.

    Returns False if the realtime session isn't available or the turn is empty.
    """
    rt = resolve_realtime_session(session)
    pcm = event.audio_pcm16
    if rt is None or not pcm:
        return False
    for i in range(0, len(pcm), _STEP):
        chunk = pcm[i:i + _STEP]
        rt.push_audio(rtc.AudioFrame(chunk, _SR, 1, len(chunk) // 2))
    rt.commit_audio()
    if instructions:
        session.generate_reply(instructions=instructions)
    else:
        session.generate_reply()
    return True
