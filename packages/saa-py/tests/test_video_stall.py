"""Server video stall -> audio-only reconnect for AttentionClient.

Deterministic: no real camera, mic, or network. The captures and the blocking
WS open are fakes, every connect records the URL it resolved, the reconnect
backoff is zeroed, and the server's messages and close are driven straight
into the socket handlers.
"""
from __future__ import annotations

import json
import threading
import urllib.parse

import pytest

import saa.client as client_mod
from saa.client import AttentionClient

JPEG = b"\xff\xd8\xff\xe0" + bytes(16) + b"\xff\xd9"


class _FakeCamera:
    instances: list["_FakeCamera"] = []

    def __init__(self, config, on_jpeg):
        self.on_jpeg = on_jpeg
        self.stopped = False
        _FakeCamera.instances.append(self)

    def start(self):
        pass

    def is_open(self):
        return True

    def stop(self):
        self.stopped = True


class _FakeMic:
    def __init__(self, config, on_pcm16):
        pass

    def start(self):
        pass

    def stop(self):
        pass


class _FakeWS:
    def __init__(self):
        self.binary: list[bytes] = []

    def send(self, data, opcode=None):
        if isinstance(data, (bytes, bytearray)):
            self.binary.append(bytes(data))

    def close(self):
        pass


@pytest.fixture
def urls(monkeypatch):
    """Patch the devices and the WS open. Returns the URL of every connect."""
    _FakeCamera.instances = []
    monkeypatch.setattr(client_mod, "CameraCapture", _FakeCamera)
    monkeypatch.setattr(client_mod, "MicCapture", _FakeMic)
    monkeypatch.setattr(client_mod.random, "uniform", lambda a, b: 0.0)
    seen: list[str] = []

    def fake_open_ws(self):
        self._sent_video = 0
        seen.append(self._resolve_ws_url_inner())
        ws = _FakeWS()
        self._ws = ws
        self._on_ws_open(ws)

    monkeypatch.setattr(AttentionClient, "_open_ws_blocking", fake_open_ws)
    return seen


def _record(client):
    log: list[tuple[str, object]] = []
    reconnected = threading.Event()
    client.on_error(lambda e: log.append(("error", e)))
    client.on_disconnected(lambda e: log.append(("disconnected", e)))
    client.on_reconnecting(lambda e: log.append(("reconnecting", e)))

    def on_reconnected(e):
        log.append(("reconnected", e))
        reconnected.set()

    client.on_reconnected(on_reconnected)
    return log, reconnected


def _errors(log):
    return [e for name, e in log if name == "error"]


def _profiles(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("server_profile", [])


def test_coded_stall_reconnects_audio_only(urls):
    client = AttentionClient(url="wss://backend.example/ws")
    log, reconnected = _record(client)
    client.start()
    try:
        cam, ws = _FakeCamera.instances[0], client._ws
        cam.on_jpeg(JPEG)
        assert client._sent_video == 1

        client._on_ws_message(ws, json.dumps({
            "type": "error", "code": "video_stall",
            "message": "Video stopped for 16 s while audio is flowing.",
        }))
        (err,) = _errors(log)
        assert (err.title, err.kind, err.retriable) == ("Video stalled", "video", True)
        assert err.message.endswith("Reconnecting audio-only.")
        assert err.detail.startswith("Video stopped for 16 s")
        # the camera is released at once, and a frame still in flight is dropped
        assert cam.stopped and client._cam is None
        assert client.enable_video is False
        cam.on_jpeg(JPEG)
        assert client._sent_video == 1

        client._on_ws_close(ws, 1011, "video stall")
        assert reconnected.wait(5)
        # the close repeats what the coded error said: no second error
        assert [name for name, _ in log] == ["error", "disconnected", "reconnecting", "reconnected"]
        assert _profiles(urls[0]) == []
        assert _profiles(urls[1]) == ["audio_only"]
    finally:
        client.stop()


@pytest.mark.parametrize("frames, title", [(0, "No video received"), (3, "Video stalled")])
def test_close_only_stall_replaces_an_explicit_profile(urls, frames, title):
    # feed mode reaches a video profile only through an explicit profile; the
    # fallback must override it, and the one already in the URL
    client = AttentionClient(url="wss://backend.example/ws?server_profile=default",
                             enable_video=False, server_profile="default")
    log, reconnected = _record(client)
    client.start()
    try:
        for _ in range(frames):
            client.feed_video(JPEG)
        assert client._sent_video == frames

        client._on_ws_close(client._ws, 1011, "video stall")
        assert reconnected.wait(5)
        (err,) = _errors(log)
        assert (err.title, err.kind, err.code, err.retriable) == (title, "video", 1011, True)
        assert err.detail == "close code 1011 (video stall)"
        assert _profiles(urls[0]) == ["default"]
        assert _profiles(urls[1]) == ["audio_only"]

        # fed frames are dropped from now on, without raising
        client.feed_video(JPEG)
        assert client._ws.binary == []
    finally:
        client.stop()


def test_other_1011_reconnects_on_the_same_profile(urls):
    client = AttentionClient(url="wss://backend.example/ws")
    log, reconnected = _record(client)
    client.start()
    try:
        client._on_ws_close(client._ws, 1011, "keepalive ping timeout")
        assert reconnected.wait(5)
        assert _errors(log) == []
        assert urls[1] == urls[0]
        assert client.enable_video is True and client._cam is not None
    finally:
        client.stop()


def test_next_start_restores_video(urls):
    client = AttentionClient(url="wss://backend.example/ws")
    client.start()
    client._on_ws_message(client._ws, json.dumps({"type": "error", "code": "no_video", "message": "m"}))
    assert client._cam is None
    client.stop()

    client.start()
    try:
        assert client.enable_video is True and client._cam is not None
        assert _profiles(urls[-1]) == []
    finally:
        client.stop()


def test_without_auto_reconnect_the_session_ends(urls):
    client = AttentionClient(url="wss://backend.example/ws", auto_reconnect=False)
    log, _ = _record(client)
    client.start()
    try:
        client._on_ws_message(client._ws, json.dumps({"type": "error", "code": "no_video", "message": "m"}))
        client._on_ws_close(client._ws, 1011, "video stall")
        video, closed = _errors(log)
        assert (video.title, video.kind) == ("No video received", "video")
        assert video.message.endswith("Video stays off until the next start().")
        assert (closed.title, closed.code) == ("Disconnected", 1011)
        assert "reconnecting" not in [name for name, _ in log]
        assert len(urls) == 1
    finally:
        client.stop()


def test_uncoded_server_error_is_unchanged(urls):
    client = AttentionClient(url="wss://backend.example/ws")
    log, _ = _record(client)
    client.start()
    try:
        client._on_ws_message(client._ws, json.dumps({"type": "error", "message": "Server full"}))
        (err,) = _errors(log)
        assert (err.title, err.kind, err.message) == ("Server Error", "server", "Server full")
        assert client._video_off is False and client._cam is not None
    finally:
        client.stop()
