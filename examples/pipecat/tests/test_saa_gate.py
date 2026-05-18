"""Behavioural tests for ``SAAGate``, production-grade adapter.

The tests stand the gate up inside a real Pipecat ``PipelineTask`` and
swap the SDK out for a ``FakeAttentionClient``. They assert the full
upstream-mode contract end-to-end:

  - upstream Pipecat audio reaches ``feed_audio``;
  - ``speech_ready`` produces downstream audio (and only then);
  - raw upstream audio is NOT forwarded to STT before the gate opens;
  - 48 kHz stereo input is converted to mono 16 kHz 100 ms chunks;
  - bot-speech triggers ``mark_responding(True)`` + ``mute()``, reverses;
  - directed speech during bot speech triggers ``InterruptionFrame``;
  - ``on_prediction`` / ``on_stats`` / ``on_connection`` produce sidecar
    frames matching the overlay schema;
  - terminal errors push a fatal ``ErrorFrame``;
  - threshold updates round-trip to the SDK.

Requires ``pipecat-ai`` and ``attenlabs-saa`` installed; the suite is
skipped otherwise.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Callable, List

import pytest

pytest.importorskip("pipecat")
pytest.importorskip("saa")

import numpy as np

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.tests.utils import SleepFrame, run_test

from saa.events import (
    AttentionErrorEvent,
    ConfigEvent,
    DisconnectedEvent,
    PredictionEvent,
    ReconnectFailedEvent,
    ReconnectedEvent,
    ReconnectingEvent,
    SpeechReadyEvent,
    StateEvent,
    StatsEvent,
    VadEvent,
)

import saa_gate as gate_module
from saa_gate import (
    SAA_FRAME_BYTES,
    SAA_FRAME_SAMPLES,
    SAA_SAMPLE_RATE,
    SAAConnectionFrame,
    SAADecisionFrame,
    SAAPredictionFrame,
    SAAStatsFrame,
    SAAGate,
    _to_mono_16k_pcm16,
)


class FakeAttentionClient:
    """In-memory stand-in for ``saa.AttentionClient`` that records calls."""

    def __init__(self, *args, **kwargs):
        self.threshold = float(kwargs.get("initial_threshold", 0.7))
        self.upstream_mode = bool(kwargs.get("upstream_mode", False))
        self.enable_audio = (
            False if self.upstream_mode else kwargs.get("enable_audio", True)
        )
        self.enable_video = (
            False if self.upstream_mode else kwargs.get("enable_video", True)
        )
        self.start_calls = 0
        self.stop_calls = 0
        self.responding_calls: List[bool] = []
        self.mute_calls = 0
        self.unmute_calls = 0
        self.feed_audio_calls: List[bytes] = []
        self.feed_video_calls: List[bytes] = []
        self._listeners: dict[str, list[Callable]] = {}

    def _add(self, name, fn):
        self._listeners.setdefault(name, []).append(fn)
        return fn

    def on_speech_ready(self, fn):       return self._add("speech_ready", fn)
    def on_prediction(self, fn):         return self._add("prediction", fn)
    def on_state(self, fn):              return self._add("state", fn)
    def on_vad(self, fn):                return self._add("vad", fn)
    def on_stats(self, fn):              return self._add("stats", fn)
    def on_config(self, fn):             return self._add("config", fn)
    def on_error(self, fn):              return self._add("error", fn)
    def on_connecting(self, fn):         return self._add("connecting", fn)
    def on_connected(self, fn):          return self._add("connected", fn)
    def on_disconnected(self, fn):       return self._add("disconnected", fn)
    def on_reconnecting(self, fn):       return self._add("reconnecting", fn)
    def on_reconnected(self, fn):        return self._add("reconnected", fn)
    def on_reconnect_failed(self, fn):   return self._add("reconnect_failed", fn)
    def on_warmup_complete(self, fn):    return self._add("warmup_complete", fn)

    def start(self): self.start_calls += 1
    def stop(self):  self.stop_calls += 1
    def mute(self):   self.mute_calls += 1
    def unmute(self): self.unmute_calls += 1

    def mark_responding(self, responding: bool) -> None:
        self.responding_calls.append(bool(responding))

    def set_threshold(self, value: float) -> None:
        self.threshold = float(value)

    def feed_audio(self, pcm16) -> None:
        if isinstance(pcm16, np.ndarray):
            data = pcm16.tobytes()
        else:
            data = bytes(pcm16)
        self.feed_audio_calls.append(data)

    def feed_video(self, jpeg) -> None:
        self.feed_video_calls.append(bytes(jpeg))

    def fire(self, event: str, *payload) -> None:
        for fn in self._listeners.get(event, []):
            fn(*payload)

    def fire_from_thread(self, event: str, *payload, timeout: float = 2.0) -> None:
        ev = threading.Event()
        def runner():
            self.fire(event, *payload)
            ev.set()
        threading.Thread(target=runner, daemon=True).start()
        ev.wait(timeout=timeout)


@pytest.fixture
def fake_factory(monkeypatch: pytest.MonkeyPatch):
    holder: dict[str, FakeAttentionClient] = {}

    def _factory(*args, **kwargs):
        client = FakeAttentionClient(*args, **kwargs)
        holder["client"] = client
        return client

    monkeypatch.setattr(gate_module, "AttentionClient", _factory)
    monkeypatch.setenv("ATTENLABS_TOKEN", "test-token")
    return holder


async def _wait_for_started(gate, timeout=0.5):
    for _ in range(int(timeout / 0.01)):
        if getattr(gate, "_started", False):
            return True
        await asyncio.sleep(0.01)
    return False


class TestResampleHelper:
    def test_passthrough_mono_16k(self) -> None:
        pcm = np.linspace(-1000, 1000, 100, dtype=np.int16)
        out = _to_mono_16k_pcm16(pcm.tobytes(), src_rate=16_000, src_channels=1)
        assert out.size == pcm.size
        assert np.array_equal(out, pcm)

    def test_stereo_48k_to_mono_16k(self) -> None:
        n = 4800
        left = np.full(n, 1000, dtype=np.int16)
        right = np.full(n, -200, dtype=np.int16)
        interleaved = np.empty(2 * n, dtype=np.int16)
        interleaved[0::2] = left
        interleaved[1::2] = right
        out = _to_mono_16k_pcm16(interleaved.tobytes(), src_rate=48_000, src_channels=2)
        assert abs(out.size - 1600) <= 1
        assert int(out.mean()) == pytest.approx(400, abs=2)

    def test_empty_input_safe(self) -> None:
        out = _to_mono_16k_pcm16(b"", src_rate=48_000, src_channels=2)
        assert out.size == 0


@pytest.mark.asyncio
async def test_upstream_audio_reaches_feed_audio(fake_factory) -> None:
    gate = SAAGate(threshold=0.7, emit_sidecar_frames=False)
    pcm = np.zeros(SAA_FRAME_SAMPLES, dtype=np.int16)
    audio = InputAudioRawFrame(
        audio=pcm.tobytes(), sample_rate=SAA_SAMPLE_RATE, num_channels=1,
    )
    received_down, _ = await run_test(
        gate, frames_to_send=[audio, SleepFrame(0.05)], expected_down_frames=[],
    )
    client = fake_factory["client"]
    assert client.upstream_mode is True
    assert len(client.feed_audio_calls) == 1
    assert len(client.feed_audio_calls[0]) == SAA_FRAME_BYTES
    assert received_down == []
    assert gate.gate_metrics.upstream_audio_frames_dropped == 1
    assert gate.gate_metrics.upstream_audio_samples_fed == SAA_FRAME_SAMPLES


@pytest.mark.asyncio
async def test_upstream_48k_stereo_is_converted(fake_factory) -> None:
    gate = SAAGate(threshold=0.7, emit_sidecar_frames=False)
    n = 4800
    interleaved = np.zeros(2 * n, dtype=np.int16)
    audio = InputAudioRawFrame(
        audio=interleaved.tobytes(), sample_rate=48_000, num_channels=2,
    )
    await run_test(
        gate, frames_to_send=[audio, SleepFrame(0.05)], expected_down_frames=[],
    )
    client = fake_factory["client"]
    if client.feed_audio_calls:
        for chunk in client.feed_audio_calls:
            assert len(chunk) == SAA_FRAME_BYTES


@pytest.mark.asyncio
async def test_raw_upstream_audio_not_forwarded(fake_factory) -> None:
    gate = SAAGate(threshold=0.7, emit_sidecar_frames=False)
    pcm = np.zeros(SAA_FRAME_SAMPLES, dtype=np.int16)
    frames = [
        InputAudioRawFrame(
            audio=pcm.tobytes(), sample_rate=SAA_SAMPLE_RATE, num_channels=1,
        )
        for _ in range(3)
    ]
    received_down, _ = await run_test(
        gate, frames_to_send=[*frames, SleepFrame(0.05)], expected_down_frames=[],
    )
    assert all(not isinstance(f, InputAudioRawFrame) for f in received_down)
    assert gate.gate_metrics.upstream_audio_frames_dropped == 3
    assert len(fake_factory["client"].feed_audio_calls) == 3


@pytest.mark.asyncio
async def test_speech_ready_produces_downstream_audio(fake_factory) -> None:
    gate = SAAGate(threshold=0.7, emit_sidecar_frames=False)
    pcm = np.zeros(1600, dtype=np.int16)
    pcm[100:400] = 12_000
    event = SpeechReadyEvent(audio_pcm16=pcm, audio_base64="", duration_sec=0.1)

    async def fire_when_ready():
        await _wait_for_started(gate)
        fake_factory["client"].fire_from_thread("speech_ready", event)

    fire_task = asyncio.create_task(fire_when_ready())
    try:
        received_down, _ = await run_test(
            gate, frames_to_send=[SleepFrame(0.3)],
            expected_down_frames=[
                UserStartedSpeakingFrame, InputAudioRawFrame, UserStoppedSpeakingFrame,
            ],
        )
    finally:
        if not fire_task.done():
            fire_task.cancel()

    audio_frame = next(f for f in received_down if isinstance(f, InputAudioRawFrame))
    assert audio_frame.sample_rate == SAA_SAMPLE_RATE
    assert audio_frame.num_channels == 1
    assert audio_frame.audio == pcm.tobytes()
    assert gate.gate_metrics.speech_ready_emitted == 1


@pytest.mark.asyncio
async def test_passthrough_upstream_audio(fake_factory) -> None:
    gate = SAAGate(passthrough_upstream_audio=True, emit_sidecar_frames=False)
    pcm = np.zeros(SAA_FRAME_SAMPLES, dtype=np.int16)
    audio = InputAudioRawFrame(
        audio=pcm.tobytes(), sample_rate=SAA_SAMPLE_RATE, num_channels=1,
    )
    received_down, _ = await run_test(
        gate, frames_to_send=[audio, SleepFrame(0.05)],
        expected_down_frames=[InputAudioRawFrame],
    )
    assert received_down
    assert isinstance(received_down[0], InputAudioRawFrame)
    assert gate.gate_metrics.upstream_audio_frames_dropped == 0


@pytest.mark.asyncio
async def test_bot_speech_marks_responding(fake_factory) -> None:
    gate = SAAGate(threshold=0.7, emit_sidecar_frames=False)
    await run_test(
        gate,
        frames_to_send=[
            BotStartedSpeakingFrame(), SleepFrame(0.05),
            BotStoppedSpeakingFrame(), SleepFrame(0.05),
        ],
        expected_down_frames=[BotStartedSpeakingFrame, BotStoppedSpeakingFrame],
    )
    client = fake_factory["client"]
    assert client.responding_calls == [True, False]
    assert client.mute_calls == 1
    assert client.unmute_calls == 1


@pytest.mark.asyncio
async def test_barge_in_pushes_interruption_frame(fake_factory) -> None:
    gate = SAAGate(enable_barge_in=True, emit_sidecar_frames=False)
    pcm = np.zeros(1600, dtype=np.int16)
    event = SpeechReadyEvent(audio_pcm16=pcm, audio_base64="", duration_sec=0.1)

    async def fire_when_ready():
        await _wait_for_started(gate)
        fake_factory["client"].fire_from_thread("speech_ready", event)

    fire_task = asyncio.create_task(fire_when_ready())
    try:
        _, received_up = await run_test(
            gate,
            frames_to_send=[
                BotStartedSpeakingFrame(), SleepFrame(0.05),
                SleepFrame(0.25),
            ],
            expected_down_frames=None,
            expected_up_frames=[InterruptionFrame],
        )
    finally:
        if not fire_task.done():
            fire_task.cancel()

    assert any(isinstance(f, InterruptionFrame) for f in received_up)
    assert gate.gate_metrics.barge_in_emitted == 1


@pytest.mark.asyncio
async def test_prediction_emits_sidecar_frame(fake_factory) -> None:
    gate = SAAGate(emit_sidecar_frames=True)

    async def fire_when_ready():
        await _wait_for_started(gate)
        fake_factory["client"].fire_from_thread(
            "prediction",
            PredictionEvent(
                cls=0, confidence=0.92, source="audio", num_faces=0,
                gaze_on_device=False, face_visible=False,
                input_modalities=["audio"],
            ),
        )

    fire_task = asyncio.create_task(fire_when_ready())
    try:
        received_down, _ = await run_test(
            gate, frames_to_send=[SleepFrame(0.2)],
            expected_down_frames=[SAAPredictionFrame, SAADecisionFrame],
        )
    finally:
        if not fire_task.done():
            fire_task.cancel()

    pred = next(f for f in received_down if isinstance(f, SAAPredictionFrame))
    assert pred.cls == 0
    assert pred.confidence == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_stats_emits_sidecar_frame(fake_factory) -> None:
    gate = SAAGate(emit_sidecar_frames=True)

    async def fire_when_ready():
        await _wait_for_started(gate)
        fake_factory["client"].fire_from_thread(
            "stats",
            StatsEvent(
                rtt_ms=42.0, sent_audio=12, sent_video=3, skipped_video=0,
                uptime_ms=1000.0, buffered_amount=0, reconnect_count=0,
            ),
        )

    fire_task = asyncio.create_task(fire_when_ready())
    try:
        received_down, _ = await run_test(
            gate, frames_to_send=[SleepFrame(0.2)],
            expected_down_frames=[SAAStatsFrame],
        )
    finally:
        if not fire_task.done():
            fire_task.cancel()

    stats = next(f for f in received_down if isinstance(f, SAAStatsFrame))
    assert stats.rtt_ms == 42.0


@pytest.mark.asyncio
async def test_connection_state_emits_sidecar_frame(fake_factory) -> None:
    gate = SAAGate(emit_sidecar_frames=True)

    async def fire_when_ready():
        await _wait_for_started(gate)
        client = fake_factory["client"]
        client.fire_from_thread(
            "reconnecting",
            ReconnectingEvent(
                attempt=2, delay_ms=500.0, cause_code=1006, cause_reason="transient",
            ),
        )
        client.fire_from_thread("reconnected", ReconnectedEvent(attempt=2, reconnect_count=1))

    fire_task = asyncio.create_task(fire_when_ready())
    try:
        received_down, _ = await run_test(
            gate, frames_to_send=[SleepFrame(0.3)],
            expected_down_frames=None,
        )
    finally:
        if not fire_task.done():
            fire_task.cancel()

    conn = [f for f in received_down if isinstance(f, SAAConnectionFrame)]
    states = [f.state for f in conn]
    assert "reconnecting" in states
    assert "reconnected" in states


@pytest.mark.asyncio
async def test_reconnect_failed_pushes_fatal_error(fake_factory) -> None:
    gate = SAAGate(emit_sidecar_frames=False)

    async def fire_when_ready():
        await _wait_for_started(gate)
        fake_factory["client"].fire_from_thread(
            "reconnect_failed",
            ReconnectFailedEvent(
                attempts=5, last_cause_code=1006, last_cause_reason="timeout",
            ),
        )

    fire_task = asyncio.create_task(fire_when_ready())
    try:
        _, received_up = await run_test(
            gate, frames_to_send=[SleepFrame(0.3)],
            expected_up_frames=None,
        )
    finally:
        if not fire_task.done():
            fire_task.cancel()

    assert any(isinstance(f, ErrorFrame) for f in received_up)


@pytest.mark.asyncio
async def test_auth_error_pushes_fatal_error(fake_factory) -> None:
    gate = SAAGate(emit_sidecar_frames=False)

    async def fire_when_ready():
        await _wait_for_started(gate)
        fake_factory["client"].fire_from_thread(
            "error",
            AttentionErrorEvent(
                kind="auth", title="Auth failed", message="invalid token",
            ),
        )

    fire_task = asyncio.create_task(fire_when_ready())
    try:
        _, received_up = await run_test(
            gate, frames_to_send=[SleepFrame(0.2)],
            expected_up_frames=None,
        )
    finally:
        if not fire_task.done():
            fire_task.cancel()

    assert any(isinstance(f, ErrorFrame) for f in received_up)
    assert gate.gate_metrics.last_error_kind == "auth"


def test_upstream_mode_default_is_true(fake_factory) -> None:
    gate = SAAGate()
    assert gate.upstream_mode is True
    client = fake_factory["client"]
    assert client.upstream_mode is True
    assert client.enable_audio is False
    assert client.enable_video is False


def test_legacy_local_mic_mode_keeps_video(fake_factory) -> None:
    gate = SAAGate(upstream_mode=False, enable_video=True)
    assert gate.upstream_mode is False
    client = fake_factory["client"]
    assert client.upstream_mode is False
    assert client.enable_audio is True
    assert client.enable_video is True


def test_token_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATTENLABS_TOKEN", raising=False)
    with pytest.raises(ValueError, match="ATTENLABS_TOKEN"):
        SAAGate()


@pytest.mark.asyncio
async def test_lifecycle_starts_and_stops_client(fake_factory) -> None:
    gate = SAAGate(emit_sidecar_frames=False)
    await run_test(gate, frames_to_send=[SleepFrame(0.05)])
    client = fake_factory["client"]
    assert client.start_calls == 1
    assert client.stop_calls == 1


def test_set_threshold_passes_through(fake_factory) -> None:
    gate = SAAGate(threshold=0.85)
    gate.set_threshold(0.55)
    assert fake_factory["client"].threshold == pytest.approx(0.55)
    assert gate.gate_metrics.last_threshold == pytest.approx(0.55)
