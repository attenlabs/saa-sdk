"""
SAA + Claude voice agent
========================
SAA gates the microphone so only device-directed speech reaches Claude.
Claude responds via its Messages API (text) and optional TTS (pyttsx3).

Usage
-----
    python agent.py                     # mic + webcam, audio-only TTS
    python agent.py --audio-only        # no webcam
    python agent.py --no-tts            # text output only (useful for testing)
    python agent.py --threshold 0.75    # override confidence threshold

Environment variables
---------------------
    SAA_API_KEY      attention labs API key  (required)
    ANTHROPIC_API_KEY                        (required)
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import sys
import threading
import time
from typing import Optional

import anthropic

try:
    from saa import AttentionClient
except ImportError:
    sys.exit("attenlabs-saa not found. Run: pip install attenlabs-saa")

try:
    import pyttsx3  # optional; gracefully skipped if absent
    _TTS_AVAILABLE = True
except ImportError:
    _TTS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SAA_API_KEY = os.environ.get("SAA_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

CLAUDE_MODEL = "claude-sonnet-4-6"
SYSTEM_PROMPT = (
    "You are a helpful voice assistant. "
    "Respond concisely — your reply will be read aloud. "
    "Use plain prose, no markdown, no bullet points."
)

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class ClaudeVoiceAgent:
    """Wraps AttentionClient and routes gated turns to Claude."""

    def __init__(
        self,
        *,
        saa_token: str,
        anthropic_api_key: str,
        threshold: float = 0.7,
        enable_video: bool = True,
        enable_tts: bool = True,
    ) -> None:
        self._anthropic = anthropic.Anthropic(api_key=anthropic_api_key)
        self._history: list[dict] = []
        self._responding = False
        self._lock = threading.Lock()

        self._tts: Optional[pyttsx3.Engine] = None
        if enable_tts and _TTS_AVAILABLE:
            self._tts = pyttsx3.init()
            self._tts.setProperty("rate", 165)

        self._saa = AttentionClient(
            token=saa_token,
            initial_threshold=threshold,
            enable_video=enable_video,
        )

        self._saa.on_connected(self._on_connected)
        self._saa.on_warmup_complete(self._on_warmup_complete)
        self._saa.on_turn_ready(self._on_turn_ready)
        self._saa.on_interrupt(self._on_interrupt)
        self._saa.on_prediction(self._on_prediction)
        self._saa.on_error(self._on_error)

    # ------------------------------------------------------------------
    # SAA event handlers
    # ------------------------------------------------------------------

    def _on_connected(self) -> None:
        print("[SAA] connected — waiting for model warm-up …")

    def _on_warmup_complete(self) -> None:
        print("[SAA] warm-up complete — listening")

    def _on_prediction(self, event) -> None:
        labels = {0: "silent", 1: "human", 2: "device"}
        label = labels.get(event.cls, "?")
        bar = "█" * int(event.confidence * 20)
        print(f"\r  {label:<6} {bar:<20} {event.confidence:.0%}   ", end="", flush=True)

    def _on_turn_ready(self, turn) -> None:
        """SAA has decided this utterance was addressed to the device."""
        print(f"\n[SAA] turn ready ({turn.duration_sec:.2f}s) — sending to Claude …")

        # Transcribe with Whisper via the Anthropic API is not required;
        # we pass raw base64 audio to Claude's audio input block.
        thread = threading.Thread(
            target=self._reply_to_turn,
            args=(turn,),
            daemon=True,
        )
        thread.start()

    def _on_interrupt(self, event) -> None:
        """User is taking the turn back while Claude is still speaking."""
        print(f"\n[interrupt] barge-in detected (fade {event.fade_ms} ms)")
        self._stop_responding()

    def _on_error(self, event) -> None:
        print(f"\n[error] {event.title}: {event.message}")

    # ------------------------------------------------------------------
    # Claude round-trip
    # ------------------------------------------------------------------

    def _reply_to_turn(self, turn) -> None:
        with self._lock:
            if self._responding:
                # Drop overlapping turns; shouldn't happen after interrupt but
                # be defensive.
                return
            self._responding = True

        self._saa.mute()
        self._saa.mark_responding(True)

        try:
            # Build the user message.
            # Claude supports audio input blocks (base64 PCM16 / mp3 / wav).
            # PCM16 @ 16 kHz mono is what SAA delivers directly.
            user_content = [
                {
                    "type": "text",
                    "text": (
                        "Here is what the user said. "
                        "Please reply as a helpful voice assistant."
                    ),
                },
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "audio/wav",
                        "data": _pcm16_to_wav_base64(
                            turn.audio_base64, sample_rate=16000
                        ),
                    },
                },
            ]

            self._history.append({"role": "user", "content": user_content})

            response = self._anthropic.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=self._history,
            )

            assistant_text = response.content[0].text
            self._history.append(
                {"role": "assistant", "content": assistant_text}
            )

            print(f"\n[Claude] {assistant_text}\n")
            self._speak(assistant_text)

        except Exception as exc:  # noqa: BLE001
            print(f"\n[error] Claude call failed: {exc}")
        finally:
            self._stop_responding()

    def _stop_responding(self) -> None:
        with self._lock:
            self._responding = False
        self._saa.mark_responding(False)
        self._saa.unmute()

    def _speak(self, text: str) -> None:
        if self._tts is not None:
            self._tts.say(text)
            self._tts.runAndWait()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._saa.start()

    def stop(self) -> None:
        self._saa.stop()


# ---------------------------------------------------------------------------
# PCM16 → WAV base64 helper
# ---------------------------------------------------------------------------


def _pcm16_to_wav_base64(audio_base64: str, *, sample_rate: int = 16000) -> str:
    """Wrap raw PCM16 bytes in a minimal WAV header and return as base64.

    Claude's document source type requires a proper audio container.
    SAA delivers raw PCM16 @ 16 kHz mono; we wrap it here without any
    third-party dependency.
    """
    pcm = base64.b64decode(audio_base64)
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = len(pcm)
    chunk_size = 36 + data_size

    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(chunk_size.to_bytes(4, "little"))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write((16).to_bytes(4, "little"))          # subchunk1 size
    buf.write((1).to_bytes(2, "little"))            # PCM = 1
    buf.write(num_channels.to_bytes(2, "little"))
    buf.write(sample_rate.to_bytes(4, "little"))
    buf.write(byte_rate.to_bytes(4, "little"))
    buf.write(block_align.to_bytes(2, "little"))
    buf.write(bits_per_sample.to_bytes(2, "little"))
    buf.write(b"data")
    buf.write(data_size.to_bytes(4, "little"))
    buf.write(pcm)

    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SAA + Claude voice agent")
    p.add_argument("--audio-only", action="store_true", help="disable webcam")
    p.add_argument("--no-tts", action="store_true", help="print replies only")
    p.add_argument(
        "--threshold",
        type=float,
        default=0.7,
        help="SAA confidence threshold (default 0.7)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    if not SAA_API_KEY:
        sys.exit("SAA_API_KEY is not set")
    if not ANTHROPIC_API_KEY:
        sys.exit("ANTHROPIC_API_KEY is not set")

    enable_tts = not args.no_tts
    if enable_tts and not _TTS_AVAILABLE:
        print("pyttsx3 not installed — TTS disabled. pip install pyttsx3")
        enable_tts = False

    print("SAA + Claude voice agent")
    print(f"  model     : {CLAUDE_MODEL}")
    print(f"  threshold : {args.threshold}")
    print(f"  video     : {'off' if args.audio_only else 'on'}")
    print(f"  TTS       : {'on' if enable_tts else 'off'}")
    print()

    agent = ClaudeVoiceAgent(
        saa_token=SAA_API_KEY,
        anthropic_api_key=ANTHROPIC_API_KEY,
        threshold=args.threshold,
        enable_video=not args.audio_only,
        enable_tts=enable_tts,
    )

    agent.start()

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nStopping …")
        agent.stop()


if __name__ == "__main__":
    main()
