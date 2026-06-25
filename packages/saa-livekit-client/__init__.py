"""saa-livekit-client — consume saa events inside a LiveKit voice agent.

Public exports:

    AttentionEngine          - listens to the hidden hosted agent's data
                               channel events and fires typed callbacks.
    start_attention_session  - summons the hosted agent into your room.
    attention_agent_token    - issues the hidden-participant token the
                               hosted agent uses to connect.
    build_attention_entrypoint - factory that composes the above into a
                                 ready-to-go entrypoint(ctx) function.

Event types:
    PredictionEvent, VADEvent, TurnReadyEvent, TurnFrame,
    InterruptEvent, InterjectionEvent, ErrorEvent
"""
from .api import (
    AttentionAPIError,
    SessionHandle,
    start_attention_session,
)
from .engine import AttentionEngine, AttentionStartupError, DATA_TOPIC
from .factory import build_attention_entrypoint
from .tokens import DEFAULT_AGENT_IDENTITY, attention_agent_token
from .types import (
    ErrorEvent,
    InterjectionEvent,
    InterruptEvent,
    PredictionEvent,
    TurnFrame,
    TurnReadyEvent,
    VADEvent,
)


__version__ = "0.3.2"

__all__ = [
    # Engine
    "AttentionEngine", "AttentionStartupError", "DATA_TOPIC",
    # REST client
    "start_attention_session", "SessionHandle", "AttentionAPIError",
    # Tokens
    "attention_agent_token", "DEFAULT_AGENT_IDENTITY",
    # Factory
    "build_attention_entrypoint",
    # Event types
    "PredictionEvent", "VADEvent", "TurnReadyEvent", "TurnFrame",
    "InterruptEvent", "InterjectionEvent", "ErrorEvent",
    # Version
    "__version__",
]
