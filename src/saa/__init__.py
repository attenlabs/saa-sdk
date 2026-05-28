"""saa-py — Python SDK for the SD Attention Server (SAA)."""

from __future__ import annotations

from .capture import CameraConfig, MicConfig
from .client import AttentionClient
from .events import (
    AttentionErrorEvent,
    ConfigEvent,
    ConversationState,
    DisconnectedEvent,
    InterruptEvent,
    PredictionEvent,
    StateEvent,
    StatsEvent,
    TurnFrame,
    TurnReadyEvent,
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
    "AttentionErrorEvent",
    "DisconnectedEvent",
    "ConversationState",
]

__version__ = "0.3.1"
