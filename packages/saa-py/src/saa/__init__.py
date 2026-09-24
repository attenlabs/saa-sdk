"""Python SDK for the Attention Labs SAA inference server."""

from __future__ import annotations

from .capture import CameraConfig, MicConfig
from .client import AttentionClient
from .events import (
    AttentionErrorEvent,
    ConfigEvent,
    ConversationState,
    DisconnectedEvent,
    InterjectionEvent,
    InterruptEvent,
    PredictionEvent,
    ReconnectedEvent,
    ReconnectingEvent,
    StateEvent,
    StatsEvent,
    TurnFrame,
    TurnReadyEvent,
    UtteranceConfigEvent,
    UtteranceEndedEvent,
    VadEvent,
)

__all__ = [
    "AttentionClient",
    "CameraConfig",
    "MicConfig",
    "PredictionEvent",
    "VadEvent",
    "StateEvent",
    "TurnFrame",
    "TurnReadyEvent",
    "ConfigEvent",
    "StatsEvent",
    "InterruptEvent",
    "InterjectionEvent",
    "UtteranceEndedEvent",
    "UtteranceConfigEvent",
    "AttentionErrorEvent",
    "DisconnectedEvent",
    "ReconnectingEvent",
    "ReconnectedEvent",
    "ConversationState",
]

__version__ = "0.9"
