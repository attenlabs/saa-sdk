#!/usr/bin/env python3
"""CLI demo for attenlabs-saa.

Streams mic + webcam to the SD Attention Server, forwards detected speech to
OpenAI Realtime, and plays the response back through the local speaker.
"""

from __future__ import annotations

import argparse
import collections
import logging
import os
import shutil
import sys
import threading
import time

import sounddevice as sd

from saa import AttentionClient, CameraConfig, MicConfig

from llm import RealtimeLLMBridge

CLASS_LABELS = {0: "silent", 1: "human", 2: "device"}

LLM_INSTRUCTIONS = (
    "You are a helpful assistant. Respond concisely in 1 sentence. "
    "If a device/TV command is spoken to you, respond as if you were controlling a TV."
)


# ── Terminal UI ─────────────────────────────────────────────────


READY_BANNER = r"""
  ╔═══════════════════════════════════════════════════════════╗
  ║                                                           ║
  ║   ██████╗ ███████╗ █████╗ ██████╗ ██╗   ██╗██╗            ║
  ║   ██╔══██╗██╔════╝██╔══██╗██╔══██╗╚██╗ ██╔╝██║            ║
  ║   ██████╔╝█████╗  ███████║██║  ██║ ╚████╔╝ ██║            ║
  ║   ██╔══██╗██╔══╝  ██╔══██║██║  ██║  ╚██╔╝  ╚═╝            ║
  ║   ██║  ██║███████╗██║  ██║██████╔╝   ██║   ██╗            ║
  ║   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═════╝    ╚═╝   ╚═╝            ║
  ║                                                           ║
  ╚═══════════════════════════════════════════════════════════╝
"""


class _SuppressableStdout:
    """Thread-safe stdout wrapper. While `suppress` is True, only
    `direct_write()` reaches the terminal — print() calls from other
    threads are dropped so they cannot corrupt cursor positioning."""

    def __init__(self, original):
        self._original = original
        self._lock = threading.Lock()
        self.suppress = False

    def write(self, text):
        if self.suppress:
            return len(text)
        with self._lock:
            return self._original.write(text)

    def flush(self):
        self._original.flush()

    def direct_write(self, text):
        with self._lock:
            self._original.write(text)
            self._original.flush()

    def fileno(self):
        return self._original.fileno()

    def isatty(self):
        return self._original.isatty()

    def __getattr__(self, name):
        return getattr(self._original, name)


class TerminalUI:
    """Bordered in-place status frame for the SAA demo.

    Renders an N-line status panel with a header and updates each line
    in place via ANSI cursor positioning. While active, all stray stdout
    writes from other threads are suppressed so they cannot tear the frame.
    """

    APP_NAME = "ATTENTION LABS :: CONVERSATION INTELLIGENCE"
    APP_VERSION = "1.0"

    _PRED_TEXT = {
        0: "NOT_TALKING",
        1: "TALKING TO HUMAN",
        2: "TALKING TO COMPUTER",
    }
    _BUFFER_LEN = 10

    def __init__(self):
        self._lock = threading.Lock()
        self._active = False
        self._term_cols = 80
        self._status_start_row = 6
        self._status_lines: list[str] = []

        self._buffer: collections.deque = collections.deque(maxlen=self._BUFFER_LEN)
        self._assistant_state = "idle"
        self._start_time = 0.0

        if not isinstance(sys.stdout, _SuppressableStdout):
            self._stdout = _SuppressableStdout(sys.stdout)
            sys.stdout = self._stdout
        else:
            self._stdout = sys.stdout

    @property
    def active(self) -> bool:
        return self._active

    def show_ready_banner(self):
        print(READY_BANNER)

    def _bordered(self, text: str) -> str:
        max_text = self._term_cols - 6
        t = text[:max_text]
        return '║  ' + t + ' ' * (max_text - len(t)) + '  ║'

    def _draw_frame(self, num_lines: int):
        try:
            self._term_cols = max(shutil.get_terminal_size().columns, 60)
        except (OSError, ValueError):
            self._term_cols = 80

        bar = '═' * (self._term_cols - 2)
        title = f'{self.APP_NAME} v{self.APP_VERSION}'

        lines = [
            '\033[2J\033[H',
            f'╔{bar}╗',
            self._bordered(title),
            self._bordered('Press Ctrl+C to stop'),
            f'╠{bar}╣',
        ]
        self._status_start_row = len(lines) + 1
        for _ in range(num_lines):
            lines.append(self._bordered(''))
        lines.append(f'╚{bar}╝')

        self._stdout.direct_write('\n'.join(lines))

    def start_status(self, num_lines: int = 4):
        with self._lock:
            if self._active:
                return
            self._status_lines = [''] * num_lines
            self._active = True
            self._stdout.suppress = True
            self._draw_frame(num_lines)

    def end_status(self):
        with self._lock:
            if not self._active:
                return
            self._active = False
            self._stdout.suppress = False
            bottom = self._status_start_row + len(self._status_lines) + 2
            self._stdout.direct_write(f'\033[{bottom};1H\n')

    def update_status(self, line_index: int, message: str):
        with self._lock:
            if not self._active or not (0 <= line_index < len(self._status_lines)):
                return
            self._status_lines[line_index] = message
            row = self._status_start_row + line_index
            max_text = self._term_cols - 6
            t = message[:max_text]
            content = '  ' + t + ' ' * (max_text - len(t)) + '  '
            bottom = self._status_start_row + len(self._status_lines) + 1
            self._stdout.direct_write(f'\033[{row};2H{content}\033[{bottom};1H')

    # ── semantic helpers used by main.py ──

    def activate(self):
        """Show READY banner then start the bordered status frame."""
        if self._active:
            return
        self.show_ready_banner()
        self._start_time = time.time()
        self.start_status(4)

    def deactivate(self):
        self.end_status()

    def update_prediction(self, cls: int, confidence: float | None):
        pred_text = self._PRED_TEXT.get(cls, "NOT_TALKING")
        pct = (confidence or 0) * 100
        self._buffer.append(cls)
        self.update_status(0, f"CURRENT MODE : {pred_text} ({pct:.2f}%)")
        self.update_status(1, f"BUFFER       : {list(self._buffer)}")
        self.update_status(3, f"PROCESSING   : {time.time() - self._start_time:.1f}s")

    def update_conv_state(self, state: str):
        self._assistant_state = state
        self.update_status(2, f"LLM STATE    : {self._assistant_state}")

    def update_llm_state(self, state: str):
        self._assistant_state = state
        self.update_status(2, f"LLM STATE    : {self._assistant_state}")

    def log(self, msg: str):
        """Pre-activation: print to stdout. Post-activation: silently dropped
        (the source UI shows no log line)."""
        if not self._active:
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] {msg}")


# ── Device selection ────────────────────────────────────────────


def _list_microphones() -> list[dict]:
    """Return list of input audio devices."""
    devices = sd.query_devices()
    inputs = []
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            inputs.append({"index": i, "name": d["name"],
                           "channels": d["max_input_channels"],
                           "rate": d["default_samplerate"]})
    return inputs


def _list_cameras() -> list[dict]:
    """Enumerate cameras using cv2_enumerate_cameras."""
    from cv2_enumerate_cameras import enumerate_cameras
    cameras = []
    for cam in enumerate_cameras():
        cameras.append({"index": cam.index, "name": cam.name})
    return cameras


def _pick(label: str, items: list[dict], key: str = "index",
          name_key: str = "name") -> int | None:
    """Interactive numbered picker. Returns chosen value or None to skip."""
    if not items:
        print(f"  No {label} found.")
        return None

    for i, item in enumerate(items):
        extra = " | ".join(f"{k}={v}" for k, v in item.items()
                           if k not in (key, name_key))
        print(f"  [{i}] {item[name_key]}" + (f"  ({extra})" if extra else ""))
    print(f"  [s] Skip {label}")

    while True:
        choice = input(f"  Select {label} [0]: ").strip().lower()
        if choice == "s":
            return None
        if choice == "":
            return items[0][key]
        try:
            idx = int(choice)
            if 0 <= idx < len(items):
                return items[idx][key]
        except ValueError:
            pass
        print("  Invalid choice, try again.")


def _select_devices(args: argparse.Namespace) -> tuple[int | None, int | None]:
    """Interactive device selection. Returns (mic_index, camera_index)."""
    mic_index = None
    cam_index = None

    if not args.no_audio:
        print("\nAvailable microphones:")
        mics = _list_microphones()
        mic_index = _pick("microphone", mics)
        if mic_index is None:
            print("  Audio disabled.")

    if not args.no_video:
        print("\nAvailable cameras:")
        cams = _list_cameras()
        cam_index = _pick("camera", cams)
        if cam_index is None:
            print("  Video disabled.")

    print()
    return mic_index, cam_index


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="attenlabs-saa CLI demo")
    p.add_argument("--token", default=os.environ.get("SAA_API_KEY"),
                   help="SAA auth token (or set SAA_API_KEY env var)")
    p.add_argument("--url", default=None,
                   help="Override the SAA server URL (default: wss://server.attentionlabs.ai/ws)")
    p.add_argument("--openai-key", default=os.environ.get("OPENAI_API_KEY"),
                   help="OpenAI API key with Realtime access (env: OPENAI_API_KEY)")
    p.add_argument("--camera-index", type=int, default=None, help="Webcam device index (skip selector)")
    p.add_argument("--mic-device", default=None,
                   help="Mic device name or index (system default if unset)")
    p.add_argument("--threshold", type=float, default=0.85,
                   help="Device-class trigger threshold 0..1")
    p.add_argument("--no-video", action="store_true", help="Disable webcam capture")
    p.add_argument("--no-audio", action="store_true", help="Disable mic capture")
    p.add_argument("--no-llm", action="store_true",
                   help="Disable LLM stage even if --openai-key is set")
    p.add_argument("--log-level", default="WARNING",
                   help="Logging level (DEBUG, INFO, WARNING, ERROR)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Interactive device selection when flags aren't explicit
    mic_device = args.mic_device
    cam_index = args.camera_index

    if mic_device is not None:
        try:
            mic_device = int(mic_device)
        except ValueError:
            pass  # treat as device name string

    needs_mic_select = mic_device is None and not args.no_audio
    needs_cam_select = cam_index is None and not args.no_video

    if needs_mic_select or needs_cam_select:
        sel_mic, sel_cam = _select_devices(args)
        if needs_mic_select:
            mic_device = sel_mic
        if needs_cam_select:
            cam_index = sel_cam

    enable_audio = not args.no_audio and mic_device is not None
    enable_video = not args.no_video and cam_index is not None

    if not enable_audio and not enable_video:
        print("Both audio and video disabled — nothing to stream.")
        return 1

    # Set sounddevice default input device
    if enable_audio:
        sd.default.device[0] = mic_device

    if not args.token:
        print("Error: --token is required (or set SAA_API_KEY env var).")
        return 1

    client = AttentionClient(
        url=args.url,
        token=args.token,
        video=CameraConfig(device_index=cam_index if cam_index is not None else 0),
        audio=MicConfig(device=mic_device),
        initial_threshold=args.threshold,
        enable_audio=enable_audio,
        enable_video=enable_video,
    )

    ui = TerminalUI()
    warmup = {"count": 0}

    use_llm = bool(args.openai_key) and not args.no_llm
    llm: RealtimeLLMBridge | None = None
    llm_state = {"s": "idle"}

    if use_llm:
        llm = RealtimeLLMBridge(
            api_key=args.openai_key,
            instructions=LLM_INSTRUCTIONS,
        )

        def on_speaking_start():
            llm_state["s"] = "speaking"
            ui.update_llm_state("Speaking")
            ui.log("LLM speaking")
            client.mute()
            client.mark_responding(True)

        def on_speaking_end():
            llm_state["s"] = "idle"
            ui.update_llm_state("Idle")
            ui.log("LLM done")
            client.unmute()
            client.mark_responding(False)

        llm.on("speaking_start", on_speaking_start)
        llm.on("speaking_end", on_speaking_end)
        llm.on("transcript", lambda t: ui.log(f"LLM: {t[:60]}"))
        llm.on("error", lambda e: ui.log(f"LLM error: {e['title']}: {e['message']}"))
    else:
        ui.log("LLM disabled — set --openai-key or OPENAI_API_KEY to enable")

    @client.on_connected
    def _(): ui.log("ws connected")

    @client.on_started
    def _(): ui.log("server started")

    @client.on_warmup_complete
    def _():
        ui.activate()
        ui.log("warmup complete — streaming live")

    @client.on_prediction
    def _(event):
        if not ui.active:
            warmup["count"] += 1
            if warmup["count"] == 1 or warmup["count"] % 5 == 0:
                print(f"  warming up model... ({warmup['count']}/~50)")
            return
        ui.update_prediction(event.cls, event.confidence)

    @client.on_state
    def _(event):
        ui.update_conv_state(event.state)
        ui.log(f"state -> {event.state}")

    @client.on_turn_ready
    def _(event):
        ui.log(f"turn ready ({event.duration_sec:.2f}s, {len(event.frames)} frames)")
        if llm is not None:
            llm_state["s"] = "processing"
            ui.update_llm_state("Processing")
            llm.send_audio_b64(event.audio_base64, frames=event.frames)

    @client.on_stats
    def _(event):
        rtt = f"{event.rtt_ms:.0f}ms" if event.rtt_ms is not None else "n/a"
        ui.log(f"stats rtt={rtt} v={event.sent_video}(-{event.skipped_video}) a={event.sent_audio}")

    @client.on_interrupt
    def _(event):
        ui.log(f"interrupt fade_ms={event.fade_ms} conf={event.confidence:.2f}")
        if llm is not None:
            llm.interrupt(event.fade_ms)
        client.unmute()
        client.mark_responding(False)

    @client.on_error
    def _(event):
        ui.log(f"ERROR {event.title}: {event.message}"
               + (f" | {event.detail}" if event.detail else ""))

    @client.on_disconnected
    def _(event):
        ui.log(f"disconnected code={event.code} reason={event.reason or 'none'}")

    print("starting... (Ctrl-C to stop)")
    try:
        client.start()
    except Exception as e:
        print(f"start failed: {e}", file=sys.stderr)
        if llm is not None:
            llm.close()
        return 1

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        ui.deactivate()
        print("\nstopping...")
        if llm is not None:
            llm.close()
        client.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
