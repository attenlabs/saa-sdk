"""Smoke test for examples/twilio/, verifies adapter shape without installs.

Runs in CI (telephony-shape.yml) on every push. AST-parses each module and
asserts the contracts the README and protocol docs promise. No FastAPI,
Twilio, or NumPy installs required.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent


def _read(name: str) -> str:
    path = ROOT / name
    if not path.is_file():
        print(f"✗ missing file: {name}", file=sys.stderr)
        sys.exit(1)
    return path.read_text(encoding="utf-8")


def _parse(name: str) -> ast.AST:
    src = _read(name)
    try:
        return ast.parse(src, filename=name)
    except SyntaxError as e:
        print(f"✗ syntax error in {name}: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> int:
    failures: list[str] = []

    server_src = _read("server.py")
    audio_src = _read("audio.py")
    twiml_src = _read("twiml.py")
    bridge_src = _read("bridge.py")
    outbound_src = _read("outbound.py")
    oai_bridge_src = _read("bridge_openai_realtime.py")
    dg_bridge_src = _read("bridge_deepgram_openai_elevenlabs.py")

    # AST parse every module so we catch syntax errors early.
    for name in (
        "server.py", "audio.py", "twiml.py", "bridge.py", "outbound.py",
        "bridge_openai_realtime.py", "bridge_deepgram_openai_elevenlabs.py",
    ):
        _parse(name)

    # ── server.py contract ───────────────────────────────────────────
    if "audio.twilio.com" not in server_src:
        failures.append("server.py: Twilio Media Streams subprotocol \"audio.twilio.com\" not negotiated")
    if "@app.websocket" not in server_src or '"/twilio"' not in server_src:
        failures.append("server.py: WebSocket route @app.websocket(\"/twilio\") missing")
    if "@app.post" not in server_src or '"/voice"' not in server_src:
        failures.append("server.py: HTTP POST /voice (TwiML) route missing")
    if "on_speech_ready" not in server_src:
        failures.append("server.py: on_speech_ready handler not registered")
    if "upstream_mode=True" not in server_src:
        failures.append(
            "server.py: AttentionClient must be constructed with "
            "upstream_mode=True (idiomatic v1.0 SDK telephony usage)"
        )
    if "enable_audio=False" not in server_src or "enable_video=False" not in server_src:
        failures.append(
            "server.py: AttentionClient must be constructed with "
            "enable_audio=False, enable_video=False (we inject audio)"
        )
    if "feed_audio" not in server_src:
        failures.append("server.py: SDK feed_audio() hook not used")
    if "wait_ready" not in server_src:
        failures.append(
            "server.py: SDK wait_ready() not called, first 100 ms of caller "
            "audio will be dropped into the cloud warmup window"
        )
    if "mark_responding" not in server_src:
        failures.append(
            "server.py: SDK mark_responding() not wired, SAA will feedback-loop on agent TTS echo"
        )
    if "3200" not in server_src:
        failures.append(
            "server.py: SAA frame size (100 ms = 3200 PCM16 bytes) not buffered to spec"
        )
    if "streamSid" not in server_src:
        failures.append("server.py: streamSid not tracked (required for outbound media events)")
    if '"event": "clear"' not in server_src and "'event': 'clear'" not in server_src:
        failures.append(
            "server.py: Twilio Media Streams `clear` event not emitted, barge-in playback flush is broken"
        )
    if '"event": "mark"' not in server_src and "'event': 'mark'" not in server_src:
        failures.append(
            "server.py: Twilio Media Streams `mark` event not emitted, playback synchronisation unavailable"
        )
    if "set_bridge_factory" not in server_src:
        failures.append("server.py: set_bridge_factory hook missing")
    if 'X-Twilio-Signature' not in server_src and 'x-twilio-signature' not in server_src:
        failures.append("server.py: Twilio signature header not validated")
    for route in ("/health", "/ready", "/stats", "/twilio-status", "/voice/outbound"):
        if f'"{route}"' not in server_src:
            failures.append(f"server.py: route {route!r} missing")

    # ── audio.py contract ────────────────────────────────────────────
    for name in (
        "ulaw_to_pcm16",
        "pcm16_to_ulaw",
        "upsample_8k_to_16k",
        "downsample_16k_to_8k",
        "twilio_payload_to_pcm16_16k",
        "pcm16_16k_to_twilio_payload",
    ):
        if f"def {name}" not in audio_src:
            failures.append(f"audio.py: function {name} missing")
    if "import audioop" in audio_src or "audioop." in audio_src:
        failures.append(
            "audio.py: pure-NumPy codec required (audioop was removed in Python 3.13)"
        )

    # ── twiml.py contract ────────────────────────────────────────────
    for name in ("twiml_for_stream", "twiml_with_recording_disclosure", "twiml_reject"):
        if f"def {name}" not in twiml_src:
            failures.append(f"twiml.py: function {name} missing")
    if "<Stream" not in twiml_src or "</Connect>" not in twiml_src:
        failures.append("twiml.py: TwiML must emit <Connect><Stream> for bidirectional media")

    # ── bridge.py contract ───────────────────────────────────────────
    for name in ("Bridge", "CallContext", "CallSession", "LoggingBridge"):
        if f"class {name}" not in bridge_src:
            failures.append(f"bridge.py: class {name} missing")
    if "outbound_pcm16_16k" not in bridge_src:
        failures.append("bridge.py: outbound_pcm16_16k queue interface missing")
    for method in (
        "send_audio", "clear_playback", "send_mark", "hangup",
        "mark_responding", "mute", "unmute", "set_threshold",
    ):
        if f"async def {method}" not in bridge_src:
            failures.append(
                f"bridge.py: CallSession.{method} not exposed, bridges can't reach the SDK control"
            )
    for hook in (
        "on_user_speech_started", "on_mark_played", "on_saa_prediction",
        "on_saa_vad", "on_saa_warmup_complete", "on_saa_stats",
    ):
        if hook not in bridge_src:
            failures.append(
                f"bridge.py: bridge hook {hook} missing, SAA event surface not fully forwarded"
            )

    # ── reference bridges ────────────────────────────────────────────
    if "class OpenAIRealtimeBridge" not in oai_bridge_src:
        failures.append(
            "bridge_openai_realtime.py: OpenAIRealtimeBridge class missing"
        )
    if "mark_responding" not in oai_bridge_src:
        failures.append(
            "bridge_openai_realtime.py: mark_responding not wired, will feedback-loop on TTS echo"
        )
    if "class DeepgramOpenAIElevenLabsBridge" not in dg_bridge_src:
        failures.append(
            "bridge_deepgram_openai_elevenlabs.py: bridge class missing"
        )
    if "mark_responding" not in dg_bridge_src:
        failures.append(
            "bridge_deepgram_openai_elevenlabs.py: mark_responding not wired"
        )

    # ── outbound.py contract ─────────────────────────────────────────
    if "def place_call" not in outbound_src:
        failures.append("outbound.py: place_call() helper missing")
    if "calls.create" not in outbound_src:
        failures.append("outbound.py: must call Twilio REST calls.create()")

    if failures:
        for f in failures:
            print(f"✗ {f}", file=sys.stderr)
        return 1

    print(
        "✓ twilio shape: subprotocol audio.twilio.com + /voice TwiML + /twilio WebSocket + "
        "100 ms SAA frames + upstream_mode + feed_audio + wait_ready + mark_responding + "
        "clear/mark events + /health + /ready + /stats + /twilio-status + outbound dialer + "
        "signature validation + OAI Realtime ref bridge + Deepgram/OAI/EL ref bridge"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
