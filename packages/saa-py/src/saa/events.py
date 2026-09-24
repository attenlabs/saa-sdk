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
    responding: bool = False  # True while the AI is mid-playback


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
    context: Optional[str] = None  # e.g. "interjection_follow_up"; None for normal turns


@dataclass
class ConfigEvent:
    model_class2_threshold: float


@dataclass
class InterruptEvent:
    """User is barging in mid-LLM-response.

    Fires when the server detects a confident class-2 prediction while the
    LLM is speaking. The server has already moved its conversation state
    machine into ``listening`` and pre-rolled the user's recent audio into
    the next turn, so the ``turn_ready`` that follows will carry the actual
    barge-in question (not just the tail of speech captured after the fade).

    Consumers should: (a) fade and stop their local LLM playback over
    ``fade_ms``, (b) cancel any in-flight LLM response, (c) re-open the mic
    immediately — do not wait for the fade to finish, or the user's
    continued speech is dropped for the duration of the fade.
    """
    fade_ms: int        # suggested fade duration before stopping playback
    confidence: float   # raw model confidence of the firing class-2 prediction


@dataclass
class InterjectionEvent:
    """Proactive AI volunteer — humans chatted then went quiet (P3 pattern).

    The server's InterjectionDetector fired: recent conversation audio is
    handed back so the consumer can prompt its LLM for a brief volunteer.
    The SDK already self-marked its cooldown clock; no upstream ack needed.
    """
    reason: str
    audio_pcm16: np.ndarray  # int16, 16 kHz mono
    audio_base64: str
    duration_sec: float


@dataclass
class UtteranceEndedEvent:
    """One finished utterance from the utterance pipeline (opt-in via
    ``utterance_handling=True``).

    ``prediction`` is 1 (aimed at a person) or 2 (aimed at the device), or
    None when the classifier failed open (``reason == "classifier_error"``);
    ``decision`` applies the server's one-sided threshold (see
    ``UtteranceConfigEvent.class1_threshold``). ``assistant_turns == 0`` means
    no assistant lines were fed back with ``add_assistant_turn`` and the
    verdict is unreliable. While the feature is in preview, ``preview`` is True
    and the verdict is preview grade.
    """
    seq: int
    text: str
    prediction: Optional[int]
    confidence: Optional[float]
    decision: str                       # "respond" | "not_respond"
    reason: str                         # "scored" | "classifier_error"
    start_s: float                      # session clock, seconds
    end_s: float
    truncated: bool                     # hit the server's length cap; the text is a cut window
    assistant_turns: int
    preview: bool
    latency_ms: Optional[int]           # end of speech to this event, server side
    audio_pcm16: Optional[np.ndarray]   # int16, 16 kHz mono; None when the server omits audio
    audio_base64: Optional[str]


@dataclass
class UtteranceConfigEvent:
    """Sent once after ``started`` for sessions that requested utterance
    handling, and again after ``set_utterance_threshold``."""
    enabled: bool
    class1_threshold: float
    preview: bool
    reason: Optional[str] = None        # set when enabled is False


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
    code: Optional[int] = None  # numeric WS close code when applicable
    kind: Optional[str] = None  # transport | auth | rate_limit | audio | server | environment
    retriable: bool = False


@dataclass
class DisconnectedEvent:
    code: int
    reason: str
    was_clean: bool


@dataclass
class ReconnectingEvent:
    attempt: int      # 1-based attempt counter
    delay_s: float    # backoff before this attempt
    last_code: int    # close code that triggered the reconnect


@dataclass
class ReconnectedEvent:
    attempts: int     # attempts it took to reconnect
