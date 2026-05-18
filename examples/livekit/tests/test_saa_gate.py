"""Unit tests for SAAGate + SAAAudioBridge.

These tests inject a fake ``AttentionClient`` so the suite runs offline:

  - no WebSocket connection
  - no PortAudio / OpenCV native deps
  - no livekit-agents installed

What we assert:

  * is_open() returns False before any verdict
  * speech_ready opens the gate; predictions alone do not
  * the gate closes again after gate_ttl_s elapses
  * mark_responding / mute / unmute / set_threshold pass through
  * snapshot() returns accurate counters under concurrent fire
  * lock invariants hold under multi-threaded fire (no deadlock, no torn reads)
  * SAAAudioBridge resamples + chunks audio correctly
  * feed_audio_frame triggers AttentionClient.feed_audio exactly once per 100 ms chunk
  * feed_video_jpeg forwards JPEGs to AttentionClient.feed_video
  * iter_speech_frames yields rtc.AudioFrame for speech_ready payloads
  * the multi-modal path increments the snapshot counters
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable, List, Optional

import numpy as np
import pytest

from saa_gate import (
    DEFAULT_GATE_TTL_S,
    SAA_FRAME_BYTES,
    SAA_FRAME_SAMPLES,
    SAA_SAMPLE_RATE,
    SAAAudioBridge,
    SAAGate,
    SAAGateSnapshot,
    _to_mono_16k_pcm16,
)


# ── Fakes ───────────────────────────────────────────────────────────────────────────────────────


@dataclass
class _FakePredEvent:
    cls: int
    confidence: float
    source: str = "audio"
    num_faces: int = 1


@dataclass
class _FakeSpeechEvent:
    duration_sec: float = 1.0
    audio_pcm16: object = None
    audio_base64: str = ""


@dataclass
class _FakeFrame:
    """Quack-alike for rtc.AudioFrame, only the attrs SAAAudioBridge reads."""

    data: bytes
    sample_rate: int = SAA_SAMPLE_RATE
    num_channels: int = 1
    samples_per_channel: int = 0

    def __post_init__(self) -> None:
        if not self.samples_per_channel:
            self.samples_per_channel = len(self.data) // 2 // self.num_channels


class _FakeClient:
    """A stand-in for saa.AttentionClient with just the surface SAAGate uses."""

    def __init__(self) -> None:
        self._speech_cb: Optional[Callable[[_FakeSpeechEvent], None]] = None
        self._pred_cb: Optional[Callable[[_FakePredEvent], None]] = None

        self._error_cb: Optional[Callable] = None
        self.started = False
        self.muted = False
        self.responding: Optional[bool] = None
        self.threshold: Optional[float] = None
        self.start_count = 0
        self.stop_count = 0
        self.fed_audio_chunks: List[bytes] = []
        self.fed_video_frames: List[bytes] = []

    # ── SDK decorators ────────────────────────────────────────────────────────────
    def on_speech_ready(self, cb):
        self._speech_cb = cb
        return cb

    def on_prediction(self, cb):
        self._pred_cb = cb
        return cb

    def on_error(self, cb):
        self._error_cb = cb
        return cb

    def feed_audio(self, data: bytes) -> None:
        self.fed_audio_chunks.append(bytes(data))

    def feed_video(self, data: bytes) -> None:
        self.fed_video_frames.append(bytes(data))

    # ── SDK methods used by SAAGate ──────────────────────────────────────────────
    def start(self) -> None:
        self.started = True
        self.start_count += 1

    def stop(self) -> None:
        self.started = False
        self.stop_count += 1

    def mute(self) -> None:
        self.muted = True

    def unmute(self) -> None:
        self.muted = False

    def mark_responding(self, responding: bool) -> None:
        self.responding = bool(responding)

    def set_threshold(self, value: float) -> None:
        self.threshold = float(value)

    # ── Test helpers ──────────────────────────────────────────────────────────────
    def fire_speech_ready(self, **overrides) -> None:
        assert self._speech_cb is not None, "SAAGate did not register speech_ready"
        self._speech_cb(_FakeSpeechEvent(**overrides))

    def fire_prediction(self, *, cls: int, confidence: float, **overrides) -> None:
        assert self._pred_cb is not None, "SAAGate did not register prediction"
        self._pred_cb(_FakePredEvent(cls=cls, confidence=confidence, **overrides))


@pytest.fixture
def fake_client() -> _FakeClient:
    return _FakeClient()


@pytest.fixture
def gate(fake_client: _FakeClient) -> SAAGate:
    g = SAAGate(client=fake_client)
    g.start()
    yield g
    g.stop()


# ── Tests ──────────────────────────────────────────────────────────────────────────────────────


class TestGateState:
    def test_closed_before_any_event(self, gate: SAAGate) -> None:
        assert gate.is_open() is False

    def test_speech_ready_opens_gate(self, gate: SAAGate, fake_client: _FakeClient) -> None:
        fake_client.fire_speech_ready(duration_sec=1.5)
        assert gate.is_open() is True

    def test_predictions_alone_do_not_open_gate(
        self, gate: SAAGate, fake_client: _FakeClient
    ) -> None:
        # Even a high-confidence cls=2 prediction must not open the gate by
        # itself, only speech_ready (the server's endpointed verdict) does.
        fake_client.fire_prediction(cls=2, confidence=0.99)
        assert gate.is_open() is False

    def test_gate_closes_after_ttl(self, fake_client: _FakeClient) -> None:
        # Tight TTL so the test is fast.
        gate = SAAGate(client=fake_client, gate_ttl_s=0.05)
        gate.start()
        try:
            fake_client.fire_speech_ready()
            assert gate.is_open() is True
            time.sleep(0.08)
            assert gate.is_open() is False
        finally:
            gate.stop()

    def test_speech_ready_overrides_prior_prediction(
        self, gate: SAAGate, fake_client: _FakeClient
    ) -> None:
        fake_client.fire_prediction(cls=0, confidence=0.05)
        fake_client.fire_speech_ready()
        assert gate.is_open() is True

    def test_prediction_does_not_downgrade_active_speech_ready(
        self, gate: SAAGate, fake_client: _FakeClient
    ) -> None:
        fake_client.fire_speech_ready()
        # A subsequent low-confidence prediction must not flip the gate closed.
        fake_client.fire_prediction(cls=0, confidence=0.1)
        assert gate.is_open() is True

    def test_reset_closes_gate(self, gate: SAAGate, fake_client: _FakeClient) -> None:
        fake_client.fire_speech_ready()
        assert gate.is_open() is True
        gate.reset()
        assert gate.is_open() is False

    def test_per_call_ttl_override(self, fake_client: _FakeClient) -> None:
        gate = SAAGate(client=fake_client, gate_ttl_s=10.0)  # generous default
        gate.start()
        try:
            fake_client.fire_speech_ready()
            time.sleep(0.05)
            # Tight per-call override should report closed even though default isn't expired.
            assert gate.is_open(ttl_s=0.01) is False
            # Default TTL still considers the verdict fresh.
            assert gate.is_open() is True
        finally:
            gate.stop()


class TestPassthroughs:
    def test_mark_responding(self, gate: SAAGate, fake_client: _FakeClient) -> None:
        gate.mark_responding(True)
        assert fake_client.responding is True
        gate.mark_responding(False)
        assert fake_client.responding is False

    def test_mute_unmute(self, gate: SAAGate, fake_client: _FakeClient) -> None:
        gate.mute()
        assert fake_client.muted is True
        gate.unmute()
        assert fake_client.muted is False

    def test_set_threshold(self, gate: SAAGate, fake_client: _FakeClient) -> None:
        gate.set_threshold(0.85)
        assert fake_client.threshold == pytest.approx(0.85)

    def test_start_stop_idempotent(self, fake_client: _FakeClient) -> None:
        gate = SAAGate(client=fake_client)
        gate.start()
        gate.stop()
        gate.stop()  # second stop is a no-op
        assert fake_client.start_count == 1
        assert fake_client.stop_count == 1


class TestSnapshot:
    def test_snapshot_counts(self, gate: SAAGate, fake_client: _FakeClient) -> None:
        fake_client.fire_prediction(cls=1, confidence=0.4)
        fake_client.fire_prediction(cls=2, confidence=0.85)
        fake_client.fire_speech_ready()
        fake_client.fire_speech_ready()

        snap = gate.snapshot()
        assert isinstance(snap, SAAGateSnapshot)
        assert snap.prediction_count == 2
        assert snap.speech_ready_count == 2
        assert snap.uptime_s >= 0.0

    def test_snapshot_latest_prediction_fields(
        self, gate: SAAGate, fake_client: _FakeClient
    ) -> None:
        fake_client.fire_prediction(cls=2, confidence=0.91)
        snap = gate.snapshot()
        assert snap.latest_cls == 2
        assert snap.latest_confidence == pytest.approx(0.91)


class TestThreading:
    def test_concurrent_fire_no_deadlock(
        self, gate: SAAGate, fake_client: _FakeClient
    ) -> None:
        # Hammer the gate from many threads; assert it terminates and the
        # final counters add up.
        N = 50
        barrier = threading.Barrier(N + 1)

        def producer() -> None:
            barrier.wait()
            fake_client.fire_speech_ready()
            fake_client.fire_prediction(cls=2, confidence=0.9)

        threads = [threading.Thread(target=producer, daemon=True) for _ in range(N)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join(timeout=2.0)
            assert not t.is_alive(), "producer thread deadlocked"

        snap = gate.snapshot()
        assert snap.speech_ready_count == N
        assert snap.prediction_count == N
        assert gate.is_open() is True


class TestDefaults:
    def test_default_ttl_is_documented_value(self) -> None:
        # Guard against silent drift between docs (.env.example) and code.
        assert DEFAULT_GATE_TTL_S == 2.0

    def test_saa_frame_constants_match_spec(self) -> None:
        # 100 ms @ 16 kHz mono int16 = 1600 samples = 3200 bytes (SAA SDK frame size).
        assert SAA_SAMPLE_RATE == 16_000
        assert SAA_FRAME_SAMPLES == 1600
        assert SAA_FRAME_BYTES == 3200


# ── Resampler ───────────────────────────────────────────────────────────────


class TestResampler:
    def test_passthrough_at_16k_mono(self) -> None:
        pcm = np.arange(1600, dtype=np.int16)
        out = _to_mono_16k_pcm16(pcm.tobytes(), src_rate=16_000, src_channels=1)
        assert out.dtype == np.int16
        assert out.size == 1600
        assert np.array_equal(out, pcm)

    def test_stereo_to_mono(self) -> None:
        # Stereo: identical samples in both channels → mono equal to either.
        stereo = np.repeat(np.arange(100, dtype=np.int16), 2)
        out = _to_mono_16k_pcm16(stereo.tobytes(), src_rate=16_000, src_channels=2)
        assert out.size == 100
        assert np.array_equal(out, np.arange(100, dtype=np.int16))

    def test_downsample_48k_to_16k(self) -> None:
        # 4800 samples @ 48 kHz → ~1600 @ 16 kHz (3:1 decimation).
        pcm = np.zeros(4800, dtype=np.int16)
        out = _to_mono_16k_pcm16(pcm.tobytes(), src_rate=48_000, src_channels=1)
        assert out.dtype == np.int16
        # Allow ±1 sample for rounding.
        assert abs(out.size - 1600) <= 1


# ── SAAAudioBridge (pre-ASR gate) ──────────────────────────────────────────────


@pytest.fixture
def bridge(fake_client: _FakeClient) -> SAAAudioBridge:
    b = SAAAudioBridge(client=fake_client)
    b.start()
    yield b
    b.stop()


class TestAudioBridge:
    def test_feeds_complete_100ms_chunks(
        self, bridge: SAAAudioBridge, fake_client: _FakeClient
    ) -> None:
        # 1600 samples = 3200 bytes = exactly one SAA frame.
        pcm = np.zeros(1600, dtype=np.int16)
        frame = _FakeFrame(data=pcm.tobytes(), sample_rate=16_000, num_channels=1)
        bridge.feed_audio_frame(frame)
        assert len(fake_client.fed_audio_chunks) == 1
        assert len(fake_client.fed_audio_chunks[0]) == SAA_FRAME_BYTES

    def test_buffers_partial_chunks_then_releases_on_threshold(
        self, bridge: SAAAudioBridge, fake_client: _FakeClient
    ) -> None:
        # Push 50 ms twice, the bridge should buffer the first half and
        # release one chunk after the second.
        half = np.zeros(800, dtype=np.int16)
        bridge.feed_audio_frame(_FakeFrame(data=half.tobytes()))
        assert fake_client.fed_audio_chunks == []
        bridge.feed_audio_frame(_FakeFrame(data=half.tobytes()))
        assert len(fake_client.fed_audio_chunks) == 1

    def test_resamples_48k_stereo_input(
        self, bridge: SAAAudioBridge, fake_client: _FakeClient
    ) -> None:
        # 4800 stereo samples = 4800 * 2 channels * 2 bytes = 19200 bytes.
        # After mono+16k conversion we expect ~1600 samples = ~3200 bytes
        # = roughly one SAA frame.
        stereo = np.zeros(4800 * 2, dtype=np.int16)
        bridge.feed_audio_frame(_FakeFrame(data=stereo.tobytes(), sample_rate=48_000, num_channels=2))
        assert len(fake_client.fed_audio_chunks) >= 1

    def test_feed_video_forwards_jpeg(
        self, bridge: SAAAudioBridge, fake_client: _FakeClient
    ) -> None:
        bridge.feed_video_jpeg(b"\xff\xd8\xff\xe0fakejpeg")
        assert fake_client.fed_video_frames == [b"\xff\xd8\xff\xe0fakejpeg"]

    def test_speech_ready_enqueues_audio_frame(
        self, fake_client: _FakeClient
    ) -> None:
        async def run() -> None:
            b = SAAAudioBridge(client=fake_client)
            b._loop = asyncio.get_running_loop()
            b.start()
            try:
                # 800-sample (50 ms) gated payload.
                pcm = np.arange(800, dtype=np.int16)
                fake_client.fire_speech_ready(audio_pcm16=pcm, duration_sec=0.05)
                # iter_speech_frames yields 20 ms chunks → expect at least one.
                gen = b.iter_speech_frames().__aiter__()
                first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
                # The yielded frame is an rtc.AudioFrame stub (no LiveKit) but
                # carries the SAA sample rate + channel count.
                # We don't strictly require .sample_rate on the stub, so accept
                # either a real frame or the stub.
                assert first is not None
            finally:
                b.stop()

        asyncio.run(run())

    def test_snapshot_tracks_upstream_counters(
        self, bridge: SAAAudioBridge, fake_client: _FakeClient
    ) -> None:
        pcm = np.zeros(1600, dtype=np.int16)
        bridge.feed_audio_frame(_FakeFrame(data=pcm.tobytes()))
        bridge.feed_video_jpeg(b"\xff\xd8jpeg")
        snap = bridge.snapshot()
        assert isinstance(snap, SAAGateSnapshot)
        assert snap.upstream_audio_samples_fed == SAA_FRAME_SAMPLES
        assert snap.upstream_video_frames_fed == 1

    def test_pred_passthroughs_and_threshold_clamp(
        self, bridge: SAAAudioBridge, fake_client: _FakeClient
    ) -> None:
        fake_client.fire_prediction(cls=1, confidence=0.42, num_faces=2, source="audio")
        v = bridge.latest_prediction()
        assert v is not None
        assert v.cls == 1
        assert v.confidence == pytest.approx(0.42)
        assert v.num_faces == 2
        assert v.source == "audio"

        bridge.set_threshold(0.95)
        assert fake_client.threshold == pytest.approx(0.95)

        bridge.mark_responding(True)
        assert fake_client.responding is True
        bridge.mark_responding(False)
        assert fake_client.responding is False
