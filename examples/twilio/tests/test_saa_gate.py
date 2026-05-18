"""Behavioural tests for the SAA × Twilio adapter.

These tests exercise the WebSocket handler in ``server.py`` end-to-end
with a fake ``AttentionClient`` injected via ``monkeypatch``. The fake
records every ``feed_audio`` call and lets us fire ``speech_ready``
events synchronously, so the suite runs offline (no SAA WS, no real
Twilio call, no native PortAudio / OpenCV deps).

What we assert (the upstream-mode pre-ASR contract):

  - Twilio µ-law 8 kHz audio reaches ``feed_audio`` as PCM16 16 kHz frames
  - audio is buffered into 100 ms (3200-byte) blocks before each call
  - SAA is constructed with ``enable_audio=False, enable_video=False``
  - ``speech_ready`` reaches the bridge's ``on_speech``
  - the bridge does NOT receive any audio before ``speech_ready`` fires
  - DTMF and stop events are forwarded to the bridge
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Callable, List, Optional

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("saa")

import numpy as np
from fastapi.testclient import TestClient

import server as server_module
from audio import pcm16_to_ulaw
from bridge import CallContext
from saa.events import SpeechReadyEvent


SAA_FRAME_BYTES = 3200  # 100 ms @ 16 kHz mono int16; matches server.py


class FakeAttentionClient:
    def __init__(self, *args, **kwargs):
        self.threshold = float(kwargs.get("initial_threshold", 0.7))
        self.enable_audio = kwargs.get("enable_audio", True)
        self.enable_video = kwargs.get("enable_video", True)
        self.upstream_mode = kwargs.get("upstream_mode", False)
        self.feed_audio_calls: List[bytes] = []
        self.start_calls = 0
        self.stop_calls = 0
        self.mark_responding_calls: List[bool] = []
        self.mute_calls = 0
        self.unmute_calls = 0
        self.set_threshold_calls: List[float] = []
        self._listeners: dict[str, list[Callable]] = {}

    def _decorator_for(self, name):
        def _outer(fn=None):
            def _inner(f):
                self._listeners.setdefault(name, []).append(f)
                return f
            if fn is None:
                return _inner
            return _inner(fn)
        return _outer

    def on_speech_ready(self, fn=None):      return self._decorator_for("speech_ready")(fn)
    def on_error(self, fn=None):             return self._decorator_for("error")(fn)
    def on_prediction(self, fn=None):        return self._decorator_for("prediction")(fn)
    def on_state(self, fn=None):             return self._decorator_for("state")(fn)
    def on_vad(self, fn=None):               return self._decorator_for("vad")(fn)
    def on_warmup_complete(self, fn=None):   return self._decorator_for("warmup_complete")(fn)
    def on_stats(self, fn=None):             return self._decorator_for("stats")(fn)
    def on_reconnecting(self, fn=None):      return self._decorator_for("reconnecting")(fn)
    def on_reconnected(self, fn=None):       return self._decorator_for("reconnected")(fn)
    def on_reconnect_failed(self, fn=None):  return self._decorator_for("reconnect_failed")(fn)
    def on_disconnected(self, fn=None):      return self._decorator_for("disconnected")(fn)
    def on_config(self, fn=None):            return self._decorator_for("config")(fn)
    def on_connected(self, fn=None):         return self._decorator_for("connected")(fn)
    def on_started(self, fn=None):           return self._decorator_for("started")(fn)

    def start(self): self.start_calls += 1
    def stop(self):  self.stop_calls += 1

    def wait_ready(self, timeout=None):
        return True  # the fake is always warm

    def mark_responding(self, responding: bool) -> None:
        self.mark_responding_calls.append(bool(responding))

    def mute(self) -> None:
        self.mute_calls += 1

    def unmute(self) -> None:
        self.unmute_calls += 1

    def set_threshold(self, value: float) -> None:
        self.set_threshold_calls.append(float(value))
        self.threshold = float(value)

    def feed_audio(self, pcm16) -> None:
        if isinstance(pcm16, np.ndarray):
            data = pcm16.tobytes()
        else:
            data = bytes(pcm16)
        self.feed_audio_calls.append(data)

    def fire_speech_ready(self, event: SpeechReadyEvent) -> None:
        for fn in self._listeners.get("speech_ready", []):
            fn(event)

    def fire(self, name: str, *args) -> None:
        for fn in self._listeners.get(name, []):
            fn(*args)


class RecordingBridge:
    def __init__(self) -> None:
        self.opened: Optional[CallContext] = None
        self.session = None
        self.speech: List[tuple] = []
        self.dtmf: List[str] = []
        self.barge_ins = 0
        self.hangups = 0
        self.closed = False
        self.outbound_pcm16_16k: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()

    async def open(self, ctx: CallContext, session=None) -> None:
        self.opened = ctx
        self.session = session

    async def on_speech(self, audio_pcm16_16k, duration_sec: float) -> None:
        self.speech.append((audio_pcm16_16k, duration_sec))

    async def on_user_speech_started(self) -> None:
        self.barge_ins += 1

    async def on_dtmf(self, digit: str) -> None:
        self.dtmf.append(digit)

    async def on_mark_played(self, name: str) -> None:
        pass

    async def on_saa_prediction(self, event) -> None:
        pass

    async def on_saa_vad(self, event) -> None:
        pass

    async def on_saa_warmup_complete(self) -> None:
        pass

    async def on_saa_stats(self, event) -> None:
        pass

    async def on_caller_hangup(self) -> None:
        self.hangups += 1

    async def close(self) -> None:
        await self.outbound_pcm16_16k.put(None)
        self.closed = True


@pytest.fixture
def wired_app(monkeypatch: pytest.MonkeyPatch):
    fake_holder: dict[str, FakeAttentionClient] = {}
    bridge_holder: dict[str, RecordingBridge] = {}

    def _client_factory(*args, **kwargs):
        client = FakeAttentionClient(*args, **kwargs)
        fake_holder["client"] = client
        return client

    monkeypatch.setattr(server_module, "AttentionClient", _client_factory)
    monkeypatch.setattr(server_module, "DEFAULT_TOKEN", "test-token")

    async def _make_bridge() -> RecordingBridge:
        b = RecordingBridge()
        bridge_holder["bridge"] = b
        return b

    server_module.set_bridge_factory(_make_bridge)
    return fake_holder, bridge_holder


def _twilio_media_payload(samples: int = 160) -> str:
    pcm = np.zeros(samples, dtype=np.int16)
    return base64.b64encode(pcm16_to_ulaw(pcm)).decode("ascii")


def _start_event(stream_sid: str = "MZdeadbeef") -> str:
    return json.dumps({
        "event": "start",
        "start": {
            "streamSid": stream_sid,
            "callSid": "CAdeadbeef",
            "accountSid": "ACdeadbeef",
            "customParameters": {"From": "+15551112222", "To": "+15553334444"},
        },
    })


def _media_event(stream_sid: str = "MZdeadbeef", samples: int = 160) -> str:
    return json.dumps({
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": _twilio_media_payload(samples)},
    })


def _stop_event(stream_sid: str = "MZdeadbeef") -> str:
    return json.dumps({"event": "stop", "streamSid": stream_sid})


def test_saa_constructed_with_audio_and_video_disabled(wired_app) -> None:
    fake_holder, _ = wired_app
    client = TestClient(server_module.app)
    with client.websocket_connect("/twilio", subprotocols=["audio.twilio.com"]) as ws:
        ws.send_text(_start_event())
        for _ in range(2):
            ws.send_text(_media_event())
        ws.send_text(_stop_event())

    fake = fake_holder["client"]
    assert fake.enable_audio is False
    assert fake.enable_video is False
    assert fake.start_calls == 1
    assert fake.stop_calls == 1


def test_twilio_audio_reaches_feed_audio_as_100ms_blocks(wired_app) -> None:
    fake_holder, _ = wired_app
    client = TestClient(server_module.app)
    # 16 × 20 ms (160 µ-law samples @ 8 kHz) = 320 ms of audio. After
    # decode + 8→16 kHz upsample the buffer holds 16 × 640 = 10 240 bytes
    # of PCM16, enough for three 100 ms (3200-byte) feed_audio calls.
    with client.websocket_connect("/twilio", subprotocols=["audio.twilio.com"]) as ws:
        ws.send_text(_start_event())
        for _ in range(16):
            ws.send_text(_media_event())
        ws.send_text(_stop_event())

    fake = fake_holder["client"]
    assert fake.feed_audio_calls, "Twilio media never reached AttentionClient.feed_audio"
    assert len(fake.feed_audio_calls) == 3
    for chunk in fake.feed_audio_calls:
        assert len(chunk) == SAA_FRAME_BYTES


def test_no_speech_to_bridge_before_speech_ready(wired_app) -> None:
    _, bridge_holder = wired_app
    client = TestClient(server_module.app)
    with client.websocket_connect("/twilio", subprotocols=["audio.twilio.com"]) as ws:
        ws.send_text(_start_event())
        for _ in range(8):
            ws.send_text(_media_event())
        ws.send_text(_stop_event())

    bridge = bridge_holder["bridge"]
    assert bridge.speech == []
    assert bridge.opened is not None
    assert bridge.opened.stream_sid == "MZdeadbeef"
    assert bridge.hangups == 1
    assert bridge.closed is True


def test_speech_ready_dispatches_to_bridge(wired_app) -> None:
    fake_holder, bridge_holder = wired_app
    client = TestClient(server_module.app)
    pcm = np.zeros(1600, dtype=np.int16)
    pcm[100:400] = 5000
    event = SpeechReadyEvent(audio_pcm16=pcm, audio_base64="", duration_sec=0.1)

    with client.websocket_connect("/twilio", subprotocols=["audio.twilio.com"]) as ws:
        ws.send_text(_start_event())
        for _ in range(3):
            ws.send_text(_media_event())
        fake_holder["client"].fire_speech_ready(event)
        for _ in range(2):
            ws.send_text(_media_event())
        ws.send_text(_stop_event())

    bridge = bridge_holder["bridge"]
    assert len(bridge.speech) == 1
    audio, dur = bridge.speech[0]
    assert dur == pytest.approx(0.1)
    assert audio.size == pcm.size
    assert int(audio[200]) == 5000


def test_dtmf_event_forwarded(wired_app) -> None:
    _, bridge_holder = wired_app
    client = TestClient(server_module.app)
    with client.websocket_connect("/twilio", subprotocols=["audio.twilio.com"]) as ws:
        ws.send_text(_start_event())
        ws.send_text(json.dumps({
            "event": "dtmf",
            "streamSid": "MZdeadbeef",
            "dtmf": {"digit": "5"},
        }))
        ws.send_text(_stop_event())

    bridge = bridge_holder["bridge"]
    assert bridge.dtmf == ["5"]
