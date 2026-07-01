"""
SAA SDK – Basic Proof-of-Concept Integration
============================================
Author  : Philip (Fiverr delivery for djkimbo)
SDK     : attenlabs-saa  (pip install attenlabs-saa)
Repo    : https://github.com/attenlabs/saa-sdk
Purpose : Demonstrates the Python streaming SDK in audio-only feed mode,
          wires every event type, and surfaces failure cases clearly.

Requirements
------------
    pip install attenlabs-saa numpy

Usage
-----
    SAA_API_KEY=<your-key> python saa_integration.py

    If you do not have a key yet, get one at:
    https://attentionlabs.ai/dashboard
"""

import os
import sys
import time
import wave
import struct
import threading
import numpy as np

# ── 1. Guard: API key must be present ────────────────────────────────────────
SAA_API_KEY = os.environ.get("SAA_API_KEY", "")
if not SAA_API_KEY:
    print(
        "[FAILURE CASE 1] Missing API key.\n"
        "  Set the SAA_API_KEY environment variable before running.\n"
        "  Get a key at https://attentionlabs.ai/dashboard\n"
        "  Exiting – cannot proceed without authentication."
    )
    sys.exit(1)

# ── 2. Import SDK ─────────────────────────────────────────────────────────────
try:
    from saa import AttentionClient
except ImportError as e:
    print(
        f"[FAILURE CASE 2] SDK import failed: {e}\n"
        "  Run:  pip install attenlabs-saa\n"
        "  Requires Python 3.10+."
    )
    sys.exit(1)


# ── 3. Synthetic audio generator (replaces a real microphone) ─────────────────
def generate_sine_pcm16(duration_sec: float = 1.0,
                         freq_hz: float = 440.0,
                         sample_rate: int = 16_000) -> np.ndarray:
    """Returns a mono PCM16 sine-wave burst as a numpy int16 array."""
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    wave_f32 = np.sin(2 * np.pi * freq_hz * t).astype(np.float32)
    return (wave_f32 * 32767).astype(np.int16)


# ── 4. Build the client ───────────────────────────────────────────────────────
print("[INFO] Initialising AttentionClient (audio-only feed mode)…")
client = AttentionClient(
    token=SAA_API_KEY,
    enable_audio=False,   # We feed audio manually – no microphone access needed
    enable_video=False,   # Audio-only deployment
)

# ── 5. Register all event handlers ───────────────────────────────────────────
@client.on_connected
def on_connected():
    print("[EVENT] connected  – WebSocket open")


@client.on_started
def on_started():
    print("[EVENT] started    – server model loaded")


@client.on_warmup_complete
def on_warmup_complete():
    print("[EVENT] warmup_complete – model is producing predictions")


@client.on_prediction
def on_prediction(event):
    label = {0: "SILENT", 1: "HUMAN-directed", 2: "DEVICE-directed"}.get(
        event.cls, f"unknown({event.cls})"
    )
    print(
        f"[PREDICTION] class={label}  "
        f"confidence={event.confidence:.0%}  "
        f"source={event.source}  "
        f"faces={event.num_faces}  "
        f"responding={event.responding}"
    )


@client.on_vad
def on_vad(event):
    print(
        f"[VAD]        probability={event.probability:.2f}  "
        f"is_speech={event.is_speech}"
    )


@client.on_state
def on_state(event):
    print(f"[STATE]      conversation state → {event.state}")


@client.on_turn_ready
def on_turn_ready(turn):
    print(
        f"[TURN READY] duration={turn.duration_sec:.2f}s  "
        f"audio_bytes={len(turn.audio_base64)}  "
        f"context={turn.context}"
    )
    # ── Simulate forwarding to an STT / LLM ──────────────────────────────────
    print("[INFO]       → audio_base64 would be forwarded to your STT/LLM here")

    # ── Simulate LLM responding lifecycle ────────────────────────────────────
    client.mute()
    client.mark_responding(True)
    print("[INFO]       → LLM responding: mute() + mark_responding(True)")
    time.sleep(0.5)   # simulate LLM generation delay
    client.unmute()
    client.mark_responding(False)
    print("[INFO]       → LLM done: unmute() + mark_responding(False)")


@client.on_interrupt
def on_interrupt(event):
    print(
        f"[INTERRUPT]  User barge-in detected  "
        f"fade_ms={event.fade_ms}  confidence={event.confidence:.2f}"
    )
    client.unmute()
    client.mark_responding(False)


@client.on_stats
def on_stats(event):
    print(
        f"[STATS]      rtt={event.rtt_ms}ms  "
        f"audio_sent={event.sent_audio}  "
        f"uptime={event.uptime_s:.1f}s"
    )


@client.on_config
def on_config(event):
    print(f"[CONFIG]     server threshold confirmed → {event.model_class2_threshold}")


@client.on_error
def on_error(event):
    """
    FAILURE CASE 3 – Runtime errors reported by the server.

    Common titles observed / expected:
      • "Auth Failed"        – invalid or expired API key
      • "Connection Stalled" – network dropped mid-session
      • "Rate Limited"       – too many requests on the free tier
    """
    print(
        f"[FAILURE CASE 3 / ERROR]\n"
        f"  title  : {event.title}\n"
        f"  message: {event.message}\n"
        f"  detail : {event.detail}\n"
        f"  code   : {event.code}"
    )


@client.on_disconnected
def on_disconnected(event):
    print(
        f"[DISCONNECTED] code={event.code}  "
        f"reason={event.reason!r}  "
        f"clean={event.was_clean}"
    )


# ── 6. Start the client ───────────────────────────────────────────────────────
try:
    print("[INFO] Calling client.start()…")
    client.start()
    print("[INFO] client.start() returned – WebSocket handshake succeeded")
except Exception as exc:
    """
    FAILURE CASE 4 – Handshake / connection failure.

    Raised by start() when the WebSocket cannot be established.
    Typical causes:
      • Invalid API key (401 during the WS upgrade)
      • Network / firewall blocking wss://broker.attentionlabs.ai
      • SSL certificate error in a corporate proxy environment
    """
    print(
        f"[FAILURE CASE 4] start() raised an exception.\n"
        f"  Type   : {type(exc).__name__}\n"
        f"  Detail : {exc}\n"
        "  Check your API key and network connectivity."
    )
    sys.exit(1)


# ── 7. Feed synthetic audio ───────────────────────────────────────────────────
FEED_DURATION_SEC = 10      # total demo runtime
CHUNK_DURATION_SEC = 0.1    # 100 ms chunks (matches SDK's internal wire block)
SAMPLE_RATE = 16_000
FEED_FREQ = 440.0           # Hz – simulates speech energy

print(f"\n[INFO] Feeding {FEED_DURATION_SEC}s of synthetic audio in "
      f"{CHUNK_DURATION_SEC*1000:.0f} ms chunks…\n")

start_ts = time.time()
chunk = generate_sine_pcm16(CHUNK_DURATION_SEC, FEED_FREQ, SAMPLE_RATE)

try:
    while time.time() - start_ts < FEED_DURATION_SEC:
        # ── FAILURE CASE 5: feed_audio while enable_audio=True would raise ───
        # We constructed the client with enable_audio=False, so this is safe.
        client.feed_audio(chunk, sample_rate=SAMPLE_RATE)
        time.sleep(CHUNK_DURATION_SEC)
except Exception as exc:
    print(
        f"[FAILURE CASE 5] feed_audio() failed.\n"
        f"  {type(exc).__name__}: {exc}\n"
        "  This happens if enable_audio=True was set at construction time,\n"
        "  or if the audio format / sample_rate is unsupported."
    )

# ── 8. Threshold adjustment demo ─────────────────────────────────────────────
print("\n[INFO] Adjusting device-class threshold to 0.85…")
client.set_threshold(0.85)

# ── 9. Graceful shutdown ──────────────────────────────────────────────────────
print("\n[INFO] Stopping client…")
client.stop()
print("[INFO] Done. Integration demo complete.")
