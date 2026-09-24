"""Utterance handling surface: URL/body opt-in, event dispatch, control frames.

No network: the WS send is captured and the broker POST is stubbed.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import urllib.request

import numpy as np

from saa import AttentionClient, UtteranceConfigEvent, UtteranceEndedEvent


def _capturing_client(**kw):
    c = AttentionClient(url="ws://x/ws", **kw)
    sent: list[dict] = []
    c._send_control = lambda data: (sent.append(data), True)[1]  # type: ignore[method-assign]
    return c, sent


def test_direct_url_gets_the_flag_only_when_enabled():
    assert AttentionClient(url="ws://x/ws")._resolve_ws_url_inner() == "ws://x/ws"
    assert AttentionClient(url="ws://x/ws", utterance_handling=True)._resolve_ws_url_inner() \
        == "ws://x/ws?utterance_handling=1"
    url = AttentionClient(url="ws://x/ws", enable_video=False, utterance_handling=True)._resolve_ws_url_inner()
    assert url == "ws://x/ws?server_profile=audio_only&utterance_handling=1"


def test_broker_body_carries_the_flag(monkeypatch):
    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"url": "wss://srv/ws?utterance_handling=1"}).encode()

    def fake_urlopen(req, timeout=None):
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    c = AttentionClient(url="https://broker", token="t", utterance_handling=True)
    assert c._resolve_ws_url_inner() == "wss://srv/ws?utterance_handling=1"
    assert json.loads(captured["body"]) == {"utterance_handling": True}
    c = AttentionClient(url="https://broker", token="t", enable_video=False, utterance_handling=True)
    c._resolve_ws_url_inner()
    assert json.loads(captured["body"]) == {"server_profile": "audio_only", "utterance_handling": True}
    c = AttentionClient(url="https://broker", token="t")
    c._resolve_ws_url_inner()
    assert captured["body"] == b""  # legacy empty body unchanged


def _dispatch(c, msg: dict):
    c._on_ws_message(None, json.dumps(msg))


def test_utterance_ended_dispatch():
    c, _ = _capturing_client(utterance_handling=True)
    got: list[UtteranceEndedEvent] = []
    c.on_utterance_ended(got.append)
    pcm = np.array([1, -2], dtype=np.int16)
    _dispatch(c, {
        "type": "utterance_ended", "seq": 3, "text": "What is the weather tomorrow?", "prediction": 2,
        "confidence": 0.9999, "decision": "respond", "reason": "scored", "start_s": 10.0, "end_s": 11.5,
        "truncated": False, "assistant_turns": 1, "preview": True, "latency_ms": 900,
        "audio_base64": base64.b64encode(pcm.tobytes()).decode("ascii"),
    })
    assert len(got) == 1
    e = got[0]
    assert [f.name for f in dataclasses.fields(e)] == [
        "seq", "text", "prediction", "confidence", "decision", "reason", "start_s", "end_s",
        "truncated", "assistant_turns", "preview", "latency_ms", "audio_pcm16", "audio_base64",
    ]
    assert e.seq == 3 and e.text == "What is the weather tomorrow?" and e.prediction == 2
    assert e.confidence == 0.9999 and e.decision == "respond" and e.reason == "scored"
    assert e.end_s - e.start_s == 1.5 and e.truncated is False and e.assistant_turns == 1
    assert e.preview is True and e.latency_ms == 900 and e.audio_pcm16.tolist() == [1, -2]


def test_classifier_error_dispatch_has_nulls_and_no_audio():
    c, _ = _capturing_client()
    got: list[UtteranceEndedEvent] = []
    c.on_utterance_ended(got.append)
    _dispatch(c, {
        "type": "utterance_ended", "seq": 1, "text": "hi", "prediction": None, "confidence": None,
        "decision": "respond", "reason": "classifier_error", "start_s": 0, "end_s": 1, "truncated": True,
        "assistant_turns": 0, "preview": False, "latency_ms": None,
    })
    e = got[0]
    assert e.prediction is None and e.confidence is None and e.audio_pcm16 is None and e.audio_base64 is None
    assert e.reason == "classifier_error" and e.truncated is True and e.preview is False and e.latency_ms is None


def test_utterance_config_dispatch():
    c, _ = _capturing_client()
    got: list[UtteranceConfigEvent] = []
    c.on_utterance_config(got.append)
    _dispatch(c, {"type": "utterance_config", "enabled": True, "class1_threshold": 0.9, "preview": True})
    _dispatch(c, {"type": "utterance_config", "enabled": False, "class1_threshold": 0.97, "preview": True,
                  "reason": "no_classifier"})
    assert got[0] == UtteranceConfigEvent(enabled=True, class1_threshold=0.9, preview=True, reason=None)
    assert got[1] == UtteranceConfigEvent(enabled=False, class1_threshold=0.97, preview=True, reason="no_classifier")


def test_control_methods_send_the_utterance_actions():
    c, sent = _capturing_client()
    assert c.add_assistant_turn("  Anything else I can help with?  ") is True
    assert c.add_assistant_turn("   ") is False
    c.set_utterance_threshold(0.9)
    c.set_utterance_threshold(5)
    c.set_utterance_threshold(-1)
    c.clear_utterance_history()
    assert sent == [
        {"action": "utterance_assistant_turn", "text": "Anything else I can help with?"},
        {"action": "utterance_set_threshold", "value": 0.9},
        {"action": "utterance_set_threshold", "value": 1.0},
        {"action": "utterance_set_threshold", "value": 0.001},
        {"action": "utterance_clear_history"},
    ]


def test_add_assistant_turn_reports_false_when_not_connected():
    c = AttentionClient(url="ws://x/ws")
    assert c.add_assistant_turn("hello") is False
