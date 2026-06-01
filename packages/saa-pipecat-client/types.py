"""Typed event payloads emitted by AttentionEngine callbacks.

These mirror the JSON envelopes published on the LiveKit data channel topic
"saa" by the hosted attention agent. The engine parses incoming JSON into
these dataclasses and hands them to the consumer's callbacks.

Stable surface — keep field names backward-compatible across minor versions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


PredictionSource = Literal["model", "rules", "ai_responding"]
StateName = Literal["listening", "cancelled"]
TurnContext = Literal["interjection_follow_up", "interjection"] | None


@dataclass(frozen=True)
class PredictionEvent:
    """Per-tick model output (4 Hz)."""

    raw_class: int
    """Raw model output: 0=silent, 1=human-to-human, 2=human-to-device."""

    aligned_class: int
    """5-tick AI-aware corrected class. Use this for UI / gating — the raw
    output can transiently flip to class-2 in the 5 ticks following AI
    playback end, which `aligned_class` suppresses."""

    confidence: float
    """[0.0, 1.0]. For class-2 ticks, this is the probability the user is
    addressing the device."""

    source: PredictionSource
    """How the class was derived. `model` is normal operation; `rules` is
    a deterministic override (rare); `ai_responding` means the AI is
    speaking back and the model is gated."""

    num_faces: int
    """Faces visible on the input video tick."""


@dataclass(frozen=True)
class VADEvent:
    """Per-tick voice activity (4 Hz)."""

    is_speech: bool
    probability: float


@dataclass(frozen=True)
class TurnFrame:
    """A single JPEG sampled from the listening window.

    `ts_offset_s` is relative to listening-start (0.0 = first tick of the
    turn). Negative values are pre-context frames sampled before LISTENING
    began (`AttentionConfig.frame_pre_context_s`).
    """

    ts_offset_s: float
    jpeg_bytes: bytes


@dataclass(frozen=True)
class TurnReadyEvent:
    """Fired when the user completes a turn (class-2 streak → silence streak).

    `audio_pcm16` is int16 mono PCM at 16 kHz, ready to ship to an LLM.
    `frames` is empty unless the session was opened with
    `attention_config={"frames_per_turn": N}` where N > 0.

    `context` is None for normal turns. The string "interjection_follow_up"
    indicates this turn was captured in the forced-LISTENING window opened
    after a successful interjection — consumers should route these turns
    to a different LLM prompt (e.g., "respond 'Ok' on no/silence").
    """

    audio_pcm16: bytes
    duration: float
    frames: list[TurnFrame] = field(default_factory=list)
    context: TurnContext = None


@dataclass(frozen=True)
class InterruptEvent:
    """Fired when the InterruptDetector observes a confident class-2 streak
    during AI playback. The consumer should stop their TTS / cancel any
    in-flight LLM response and re-open the mic — the hosted agent has
    already moved its internal state machine to LISTENING and pre-rolled
    the recent ring-buffer audio into the chunk accumulator, so the next
    `TurnReadyEvent` will carry the user's barge-in question.
    """

    confidence: float


InterjectionReason = Literal["stuck_after_question"]


@dataclass(frozen=True)
class InterjectionEvent:
    """Fired when the InterjectionDetector observes the "humans were chatting,
    then went quiet, still in frame" pattern.

    `audio_pcm16` is the recent conversation audio (int16 mono 16 kHz) ending
    at the moment silence began — i.e., the LLM hears the conversation, NOT
    the silence that triggered the fire. Route to your LLM with a brief
    "offer help in one short sentence"-style instruction.
    """

    reason: InterjectionReason
    audio_pcm16: bytes
    duration: float


@dataclass(frozen=True)
class ErrorEvent:
    """Out-of-band error from the hosted agent. Final events before session
    teardown will use `code="reconnect_failed"` or similar; transient errors
    surface here for logging.
    """

    code: str
    message: str
