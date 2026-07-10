"""saa-pipecat-client — consume saa events inside a Pipecat / Daily voice agent.

Public exports:

    AttentionEngine          - listens to the hidden hosted bot's Daily
                               app-message events and fires typed callbacks.
    start_attention_session  - summons the hosted bot into your Daily room.
    attention_agent_token    - issues the hidden-participant Daily meeting
                               token the hosted bot uses to connect.
    build_attention_runner   - factory that composes the above into a
                               ready-to-go `run(...)` coroutine.

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
from .factory import build_attention_runner
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


__version__ = "0.3.4"

__all__ = [
    # Engine
    "AttentionEngine", "AttentionStartupError", "DATA_TOPIC",
    # REST client
    "start_attention_session", "SessionHandle", "AttentionAPIError",
    # Tokens
    "attention_agent_token", "DEFAULT_AGENT_IDENTITY",
    # Factory
    "build_attention_runner",
    # Event types
    "PredictionEvent", "VADEvent", "TurnReadyEvent", "TurnFrame",
    "InterruptEvent", "InterjectionEvent", "ErrorEvent",
    # Version
    "__version__",
]
