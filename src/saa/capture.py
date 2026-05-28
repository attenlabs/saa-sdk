from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

import cv2
import numpy as np

TARGET_AUDIO_RATE = 16000
SEND_INTERVAL_SAMPLES = 1600  # 100 ms @ 16 kHz
VIDEO_INTERVAL_S = 0.25

MicDevice = Union[int, str, None]


@dataclass
class MicConfig:
    device: MicDevice = None
    channels: int = 1


@dataclass
class CameraConfig:
    device_index: int = 0
    width: int = 1920
    height: int = 1080
    jpeg_quality: int = 50


class MicCapture:
    """Sounddevice input stream → int16 PCM @ 16 kHz, chunked to 100 ms blocks."""

    def __init__(self, config: MicConfig, on_pcm16: Callable[[bytes], None]):
        self.config = config
        self.on_pcm16 = on_pcm16
        self._stream: Optional[Any] = None
        self._muted = False
        self._buffer = np.zeros(0, dtype=np.float32)
        self._src_rate = TARGET_AUDIO_RATE
        self._lock = threading.Lock()

    def start(self) -> None:
        # Imported lazily so `import saa` doesn't fail on systems without
        # PortAudio installed — users who only need video capture shouldn't
        # have to install the audio stack.
        import sounddevice as sd

        device_info = sd.query_devices(
            self.config.device if self.config.device is not None else None,
            kind="input",
        )
        native_rate = int(device_info["default_samplerate"])

        # Prefer asking PortAudio for 16 kHz directly — its C-side resampler
        # is higher quality than our linear interpolator. Fall back to the
        # device's native rate + manual downsample if the host API refuses.
        try:
            self._stream = sd.InputStream(
                samplerate=TARGET_AUDIO_RATE,
                channels=self.config.channels,
                dtype="float32",
                device=self.config.device,
                callback=self._audio_callback,
            )
            self._src_rate = TARGET_AUDIO_RATE
        except Exception:
            self._stream = sd.InputStream(
                samplerate=native_rate,
                channels=self.config.channels,
                dtype="float32",
                device=self.config.device,
                callback=self._audio_callback,
            )
            self._src_rate = native_rate
        self._stream.start()

    def _audio_callback(self, indata, frames, time_info, status):
        # Keep capturing even when muted. The server-side mic_muted flag
        # (set via the "mute" control action) is what gates the LLM speech
        # accumulator; the SD-SAA inference ring buffer must keep receiving
        # live audio every tick or AudioMetricsCalculator computes
        # amplitude=0 during AI response, which is the key divergence vs
        # on-device-debug where the mic is always live.
        samples = indata[:, 0] if indata.ndim > 1 else indata.ravel()
        if self._src_rate != TARGET_AUDIO_RATE:
            samples = _linear_downsample(samples, self._src_rate, TARGET_AUDIO_RATE)

        with self._lock:
            self._buffer = np.concatenate([self._buffer, samples])
            chunks: list[np.ndarray] = []
            while len(self._buffer) >= SEND_INTERVAL_SAMPLES:
                chunks.append(self._buffer[:SEND_INTERVAL_SAMPLES])
                self._buffer = self._buffer[SEND_INTERVAL_SAMPLES:]

        for chunk in chunks:
            pcm16 = np.clip(chunk * 32768.0, -32768, 32767).astype(np.int16)
            try:
                self.on_pcm16(pcm16.tobytes())
            except Exception:
                pass

    def mute(self) -> None:
        self._muted = True

    def unmute(self) -> None:
        with self._lock:
            self._buffer = np.zeros(0, dtype=np.float32)
        self._muted = False

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


class CameraCapture:
    """OpenCV webcam → JPEG bytes at a fixed 250 ms interval.

    A reader thread continuously drains the camera so the encoder always
    snapshots the freshest available frame; without this, AVFoundation /
    V4L2 / MSMF can hand back frames buffered up to ~150 ms ago.
    """

    def __init__(self, config: CameraConfig, on_jpeg: Callable[[bytes], None]):
        self.config = config
        self.on_jpeg = on_jpeg
        self._cap: Optional[cv2.VideoCapture] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._sender_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_lock = threading.Lock()

    def start(self) -> None:
        self._cap = cv2.VideoCapture(self.config.device_index)
        if self._cap.isOpened():
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
            self._cap.set(cv2.CAP_PROP_FPS, 30)
            # Best-effort on backends that honour it (V4L2, MSMF). AVFoundation
            # ignores it, but the reader thread keeps the queue drained anyway.
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._stop.clear()
        self._latest_frame = None
        self._reader_thread = threading.Thread(
            target=self._reader, daemon=True, name="saa-camera-read")
        self._sender_thread = threading.Thread(
            target=self._sender, daemon=True, name="saa-camera-send")
        self._reader_thread.start()
        self._sender_thread.start()

    def _reader(self) -> None:
        while not self._stop.is_set():
            if self._cap is None or not self._cap.isOpened():
                time.sleep(0.01)
                continue
            ok, frame = self._cap.read()
            if not ok or frame is None:
                time.sleep(0.005)
                continue
            with self._frame_lock:
                self._latest_frame = frame

    def _sender(self) -> None:
        jpeg_params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.config.jpeg_quality)]
        next_deadline = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_deadline:
                time.sleep(min(0.05, next_deadline - now))
                continue
            next_deadline = now + VIDEO_INTERVAL_S

            with self._frame_lock:
                frame = self._latest_frame
            if frame is None:
                continue
            ok, buf = cv2.imencode(".jpg", frame, jpeg_params)
            if not ok:
                continue
            try:
                self.on_jpeg(buf.tobytes())
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        for t in (self._reader_thread, self._sender_thread):
            if t is not None:
                t.join(timeout=2.0)
        self._reader_thread = None
        self._sender_thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None


def _linear_downsample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or len(samples) == 0:
        return samples
    ratio = src_rate / dst_rate
    out_len = int(len(samples) / ratio)
    if out_len == 0:
        return np.zeros(0, dtype=samples.dtype)
    indices = np.arange(out_len) * ratio
    lo = np.floor(indices).astype(np.int64)
    hi = np.clip(lo + 1, 0, len(samples) - 1)
    frac = (indices - lo).astype(samples.dtype)
    return samples[lo] * (1 - frac) + samples[hi] * frac
