"""Camera-failure -> audio-only fallback for AttentionClient.start().

Deterministic: no real camera, mic, or network. The camera and mic captures are
replaced with fakes, and the blocking WS open is stubbed to record the state it
sees (so we can prove the audio_only profile binds at connect time).
"""
from __future__ import annotations

import logging

import pytest

import saa.client as client_mod
from saa.client import AttentionClient


class _FakeCameraCapture:
    """Stands in for CameraCapture. ``open_result`` decides whether the probed
    device reports as open, mirroring cv2.VideoCapture.isOpened()."""

    instances: list["_FakeCameraCapture"] = []
    open_result = False

    def __init__(self, config, on_jpeg):
        self.config = config
        self.on_jpeg = on_jpeg
        self.started = False
        self.stopped = False
        _FakeCameraCapture.instances.append(self)

    def start(self):
        self.started = True

    def is_open(self):
        return _FakeCameraCapture.open_result

    def stop(self):
        self.stopped = True


class _FakeMicCapture:
    def __init__(self, config, on_pcm16):
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        pass


class _FakeWS:
    def close(self):
        pass


@pytest.fixture
def patched(monkeypatch):
    """Patch camera + mic + the blocking WS open. Returns a dict capturing the
    value of enable_video observed when the WS was opened."""
    _FakeCameraCapture.instances = []
    _FakeCameraCapture.open_result = False
    monkeypatch.setattr(client_mod, "CameraCapture", _FakeCameraCapture)
    monkeypatch.setattr(client_mod, "MicCapture", _FakeMicCapture)

    observed: dict = {"ws_opened": False, "enable_video_at_open": None}

    def fake_open_ws(self):
        observed["ws_opened"] = True
        observed["enable_video_at_open"] = self.enable_video
        self._ws = _FakeWS()
        self._ws_open.set()

    monkeypatch.setattr(AttentionClient, "_open_ws_blocking", fake_open_ws)
    return observed


def test_camera_unavailable_falls_back_to_audio_only(patched, caplog):
    _FakeCameraCapture.open_result = False
    client = AttentionClient(
        url="wss://backend.example/ws", enable_video=True, enable_audio=True
    )
    with caplog.at_level(logging.WARNING, logger="saa"):
        client.start()
    try:
        # the camera was probed and then torn down — no silent spin on a dead device
        assert len(_FakeCameraCapture.instances) == 1
        cam = _FakeCameraCapture.instances[0]
        assert cam.started is True
        assert cam.stopped is True
        assert client._cam is None

        # the session flipped to audio-only
        assert client.enable_video is False

        # ...and the WS opened AFTER the flip, so the audio_only profile binds
        assert patched["ws_opened"] is True
        assert patched["enable_video_at_open"] is False

        # a clear one-line warning was logged
        assert any(
            "audio-only" in r.getMessage() for r in caplog.records
        ), "expected an audio-only fallback warning"
    finally:
        client.stop()


def test_camera_available_keeps_video(patched):
    _FakeCameraCapture.open_result = True
    client = AttentionClient(
        url="wss://backend.example/ws", enable_video=True, enable_audio=True
    )
    client.start()
    try:
        assert client.enable_video is True
        assert patched["enable_video_at_open"] is True
        cam = _FakeCameraCapture.instances[0]
        assert cam.started is True
        assert cam.stopped is False
    finally:
        client.stop()


def test_video_wish_restored_on_restart(patched):
    _FakeCameraCapture.open_result = False
    client = AttentionClient(
        url="wss://backend.example/ws", enable_video=True, enable_audio=True
    )
    client.start()
    assert client.enable_video is False  # fell back
    client.stop()

    # camera comes back for the next session
    _FakeCameraCapture.open_result = True
    client.start()
    try:
        # the original video wish was restored, so start() retried video
        assert client.enable_video is True
        assert patched["enable_video_at_open"] is True
    finally:
        client.stop()


def test_video_only_camera_failure_raises(patched):
    _FakeCameraCapture.open_result = False
    client = AttentionClient(
        url="wss://backend.example/ws", enable_video=True, enable_audio=False
    )
    # nothing to fall back to (no audio) — surface a clear error, never a silent spin
    with pytest.raises(RuntimeError, match="no media source"):
        client.start()

    assert client._started is False
    assert client._cam is None
    # the WS was never opened on a dead-media session
    assert patched["ws_opened"] is False
