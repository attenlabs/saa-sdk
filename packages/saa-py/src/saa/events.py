from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

import numpy as np

ConversationState = Literal["listening", "sending", "cancelled", "idle"]


@dataclass
class PredictionEvent:
    cls: int
    confidence: float
    source: str
    num_faces: int


@dataclass
class VadEvent:
    probability: float
    is_speech: bool


@dataclass
class StateEvent:
    state: ConversationState


@dataclass
class TurnFrame:
    """One still captured from the conversation turn.

    LLM-agnostic: SDK delivers raw base64-encoded JPEG. Callers wrap it for
    whatever LLM they target (OpenAI input_image, Anthropic image, Gemini
    inlineData, …).
    """
    ts_offset_s: float  # seconds from listening-start; negative = pre-context
    image_base64: str   # JPEG bytes, base64-encoded (no data: prefix)


@dataclass
class TurnReadyEvent:
    audio_pcm16: np.ndarray  # int16, 16 kHz mono
    audio_base64: str
    duration_sec: float
    frames: list[TurnFrame] = field(default_factory=list)
    """Empty unless the server has frames_per_turn > 0."""


@dataclass
class ConfigEvent:
    model_class2_threshold: float


@dataclass
class StatsEvent:
    rtt_ms: Optional[float]
    sent_video: int
    skipped_video: int
    sent_audio: int
    uptime_s: float


@dataclass
class AttentionErrorEvent:
    title: str
    message: str
    detail: Optional[str] = None
    code: Optional[int] = None


@dataclass
class DisconnectedEvent:
    code: int
    reason: str
    was_clean: bool
