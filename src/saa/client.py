from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

import numpy as np
import websocket

from .capture import CameraCapture, CameraConfig, MicCapture, MicConfig
from .events import (
    AttentionErrorEvent,
    ConfigEvent,
    DisconnectedEvent,
    InterruptEvent,
    PredictionEvent,
    StateEvent,
    StatsEvent,
    TurnFrame,
    TurnReadyEvent,
    VadEvent,
)
from .ws_protocol import MSG_AUDIO, MSG_VIDEO, frame_binary

# Default URL is the broker. The SDK auto-detects mode from the URL scheme:
#   http(s)://… → POST /allocate (Bearer token), then connect to the
#                 wss URL the broker returns. Recommended default — sticky
#                 least-loaded routing across multiple backends.
#   ws(s)://…   → connect WS directly (legacy / debugging). Bypasses the
#                 broker. Useful for pinning a session to a specific
#                 backend during tests.
DEFAULT_SERVER_URL = "https://broker.attentionlabs.ai"
BROKER_ALLOCATE_TIMEOUT_S = 5.0
DEFAULT_THRESHOLD = 0.7

WS_PING_INTERVAL_S = 5.0
WS_PONG_TIMEOUT_S = 15.0
WS_STATS_INTERVAL_S = 10.0
WS_HANDSHAKE_TIMEOUT_S = 10.0

logger = logging.getLogger("saa")

Listener = Callable[..., Any]


class AttentionClient:
    """Streams mic + webcam to the SAA inference server and emits typed events.

    Callbacks fire on the WebSocket receive thread (or on the heartbeat thread
    for `stats`/`error`). Keep listeners fast or hand work off to your own
    thread.
    """

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        *,
        video: Optional[CameraConfig] = None,
        audio: Optional[MicConfig] = None,
        initial_threshold: float = DEFAULT_THRESHOLD,
        enable_audio: bool = True,
        enable_video: bool = True,
    ):
        self.url = url or DEFAULT_SERVER_URL
        self.token = token
        self.video_config = video or CameraConfig()
        self.audio_config = audio or MicConfig()
        self.enable_audio = enable_audio
        self.enable_video = enable_video
        self.threshold = _clamp01(initial_threshold)

        self._listeners: dict[str, list[Listener]] = {}
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_open = threading.Event()
        self._ws_closed = threading.Event()
        self._close_info: dict = {}
        self._mic: Optional[MicCapture] = None
        self._cam: Optional[CameraCapture] = None
        self._stats_thread: Optional[threading.Thread] = None
        self._stats_stop = threading.Event()
        self._started = False

        self._sent_audio = 0
        self._sent_video = 0
        self._skipped_video = 0
        self._last_rtt_ms: Optional[float] = None
        self._last_pong_at: float = 0.0
        self._ws_opened_at: float = 0.0
        self._warmed_up = False
        self._muted = False

    # ── event registration (decorator style) ───────────────────────

    def on_connected(self, func: Listener) -> Listener: return self._register("connected", func)
    def on_started(self, func: Listener) -> Listener: return self._register("started", func)
    def on_warmup_complete(self, func: Listener) -> Listener: return self._register("warmup_complete", func)
    def on_prediction(self, func: Listener) -> Listener: return self._register("prediction", func)
    def on_vad(self, func: Listener) -> Listener: return self._register("vad", func)
    def on_state(self, func: Listener) -> Listener: return self._register("state", func)
    def on_turn_ready(self, func: Listener) -> Listener: return self._register("turn_ready", func)
    def on_config(self, func: Listener) -> Listener: return self._register("config", func)
    def on_stats(self, func: Listener) -> Listener: return self._register("stats", func)
    def on_interrupt(self, func: Listener) -> Listener: return self._register("interrupt", func)
    def on_error(self, func: Listener) -> Listener: return self._register("error", func)
    def on_disconnected(self, func: Listener) -> Listener: return self._register("disconnected", func)

    def _register(self, event: str, func: Listener) -> Listener:
        self._listeners.setdefault(event, []).append(func)
        return func

    def _emit(self, event: str, *args) -> None:
        for func in self._listeners.get(event, []):
            try:
                func(*args)
            except Exception:
                logger.exception("saa listener for '%s' raised", event)

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        if self._started:
            raise RuntimeError("AttentionClient already started")
        self._started = True
        try:
            self._open_ws_blocking()
            if self.enable_audio:
                self._mic = MicCapture(self.audio_config, on_pcm16=self._on_mic_pcm16)
                self._mic.start()
            if self.enable_video:
                self._cam = CameraCapture(self.video_config, on_jpeg=self._on_cam_jpeg)
                self._cam.start()
            self._stats_stop.clear()
            self._stats_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True, name="saa-heartbeat",
            )
            self._stats_thread.start()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if not self._started:
            return

        self._stats_stop.set()
        if self._stats_thread is not None:
            self._stats_thread.join(timeout=2.0)
            self._stats_thread = None

        if self._mic is not None:
            self._mic.stop()
            self._mic = None
        if self._cam is not None:
            self._cam.stop()
            self._cam = None

        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._ws_thread is not None:
            self._ws_thread.join(timeout=2.0)
            self._ws_thread = None
        self._ws = None

        self._started = False
        self._warmed_up = False
        self._muted = False

    # ── control ───────────────────────────────────────────────────

    def mute(self) -> None:
        self._muted = True
        if self._mic is not None:
            self._mic.mute()
        self._send_control({"action": "mute"})

    def unmute(self) -> None:
        self._muted = False
        if self._mic is not None:
            self._mic.unmute()
        self._send_control({"action": "unmute"})

    def mark_responding(self, responding: bool) -> None:
        self._send_control({
            "action": "responding_start" if responding else "responding_stop",
        })

    def set_threshold(self, value: float) -> None:
        value = _clamp01(value)
        self.threshold = value
        self._send_control({"action": "set_threshold", "value": value})

    # ── WS ────────────────────────────────────────────────────────

    def _resolve_ws_url(self) -> str:
        """Resolve self.url to a concrete wss://…/ws URL.

        - ws(s)://… is treated as a direct backend URL — returned as-is.
        - http(s)://… is treated as a broker base URL — POST /allocate
          with the bearer token, return the wss URL the broker returns.

        Called once per connect, so reconnects pick a fresh least-loaded
        backend each time. Uses urllib (stdlib) — no new deps.
        """
        url = self.url
        if url.startswith("ws://") or url.startswith("wss://"):
            return url
        allocate_url = url.rstrip("/") + "/allocate"
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        req = urllib.request.Request(
            allocate_url, method="POST", headers=headers,
            data=b"",  # POST with empty body — broker doesn't read it
        )
        try:
            with urllib.request.urlopen(req, timeout=BROKER_ALLOCATE_TIMEOUT_S) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise ConnectionError(
                f"broker /allocate failed: HTTP {e.code} {body or e.reason}"
            ) from e
        except urllib.error.URLError as e:
            raise ConnectionError(f"broker /allocate request failed: {e.reason}") from e
        ws_url = payload.get("url") if isinstance(payload, dict) else None
        if not ws_url:
            raise ConnectionError(f"broker /allocate returned no url: {payload!r}")
        return ws_url

    def _open_ws_blocking(self) -> None:
        self._ws_open.clear()
        self._ws_closed.clear()
        self._close_info = {}
        self._sent_audio = 0
        self._sent_video = 0
        self._skipped_video = 0
        self._last_rtt_ms = None
        self._ws_opened_at = 0.0
        self._warmed_up = False

        ws_url = self._resolve_ws_url()
        subprotocols = [self.token] if self.token else None
        self._ws = websocket.WebSocketApp(
            ws_url,
            subprotocols=subprotocols,
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_close=self._on_ws_close,
            on_error=self._on_ws_error,
        )

        def run_ws():
            try:
                # ping_interval=0 disables websocket-client's automatic WS-level
                # pings — we send our own JSON pings so the server sees them.
                self._ws.run_forever(ping_interval=0)
            except Exception:
                logger.exception("ws run_forever raised")

        self._ws_thread = threading.Thread(target=run_ws, daemon=True, name="saa-ws")
        self._ws_thread.start()

        opened = self._ws_open.wait(timeout=WS_HANDSHAKE_TIMEOUT_S)
        if not opened:
            # If closed during handshake, report the close reason.
            if self._ws_closed.is_set():
                info = self._close_info
                raise ConnectionError(
                    f"WebSocket closed during handshake: "
                    f"code={info.get('code')} reason={info.get('reason') or 'none'}"
                )
            try:
                self._ws.close()
            except Exception:
                pass
            raise TimeoutError(
                f"WebSocket handshake timed out after {WS_HANDSHAKE_TIMEOUT_S}s (url={self.url})"
            )

    def _on_ws_open(self, ws) -> None:
        self._ws_opened_at = time.monotonic()
        self._last_pong_at = self._ws_opened_at
        self._ws_open.set()
        self._emit("connected")

    def _on_ws_message(self, ws, message) -> None:
        if not isinstance(message, str):
            return
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        self._handle_msg(msg)

    def _on_ws_close(self, ws, code, reason) -> None:
        code = code or 0
        reason = reason or ""
        was_clean = code == 1000
        self._close_info = {"code": code, "reason": reason, "was_clean": was_clean}
        self._ws_closed.set()
        # Unblock handshake waiter if we closed before opening.
        self._ws_open.set()
        self._emit("disconnected", DisconnectedEvent(
            code=code, reason=reason, was_clean=was_clean,
        ))
        err = _close_to_error(code, reason, was_clean)
        if err is not None:
            self._emit("error", err)

    def _on_ws_error(self, ws, error) -> None:
        logger.debug("ws error: %s", error)

    def _handle_msg(self, msg: dict) -> None:
        t = msg.get("type")

        if t == "pong":
            self._last_pong_at = time.monotonic()
            client_ts = msg.get("client_ts")
            if isinstance(client_ts, (int, float)):
                self._last_rtt_ms = (time.monotonic() * 1000.0) - float(client_ts)
            return

        if t == "prediction":
            # Prefer the server's display_class (e.g. low-conf class-2 relabelled
            # to class-1). Falls back to raw `class` for older servers.
            cls = msg.get("display_class")
            if cls is None:
                cls = msg.get("class")
            if cls is None:
                cls = 0
            conf = msg.get("confidence") or 0.0
            if not self._warmed_up and conf > 0:
                self._warmed_up = True
                self._emit("warmup_complete")
            self._emit("prediction", PredictionEvent(
                cls=int(cls),
                confidence=float(conf),
                source=msg.get("source") or "",
                num_faces=int(msg.get("num_faces") or 0),
            ))
        elif t == "vad":
            self._emit("vad", VadEvent(
                probability=float(msg.get("probability") or 0.0),
                is_speech=bool(msg.get("is_speech")),
            ))
        elif t == "state":
            self._emit("state", StateEvent(state=msg.get("state") or "idle"))
        elif t == "turn_ready":
            b64 = msg.get("audio_base64") or ""
            raw_frames = msg.get("frames") or []
            frames = [
                TurnFrame(
                    ts_offset_s=float(f.get("ts_offset_s") or 0.0),
                    image_base64=str(f.get("image_base64") or ""),
                )
                for f in raw_frames
                if isinstance(f, dict) and f.get("image_base64")
            ]
            self._emit("turn_ready", TurnReadyEvent(
                audio_pcm16=_b64_to_int16(b64),
                audio_base64=b64,
                duration_sec=float(msg.get("duration") or 0.0),
                frames=frames,
            ))
        elif t == "started":
            self._emit("started")
            self._send_control({"action": "set_threshold", "value": self.threshold})
        elif t == "config":
            thr = msg.get("model_class2_threshold")
            if isinstance(thr, (int, float)):
                self.threshold = float(thr)
                self._emit("config", ConfigEvent(model_class2_threshold=float(thr)))
        elif t == "interrupt":
            fade_ms_raw = msg.get("fade_ms")
            conf_raw = msg.get("confidence")
            self._emit("interrupt", InterruptEvent(
                fade_ms=int(fade_ms_raw) if isinstance(fade_ms_raw, (int, float)) else 500,
                confidence=float(conf_raw) if isinstance(conf_raw, (int, float)) else 0.85,
            ))
        elif t == "error":
            self._emit("error", AttentionErrorEvent(
                title="Server Error",
                message=msg.get("message") or "",
                detail=msg.get("detail"),
            ))

    def _send_control(self, data: dict) -> None:
        if self._ws is None or not self._ws_open.is_set():
            return
        try:
            self._ws.send(json.dumps(data))
        except Exception:
            pass

    def _on_mic_pcm16(self, pcm16_bytes: bytes) -> None:
        # Don't gate on self._muted — keep streaming PCM during mute. The
        # "mute" control action already informs the server, which only blocks
        # the LLM chunk accumulator (so TTS isn't fed back to OpenAI as user
        # speech). The inference ring buffer needs live audio every tick.
        if self._ws is None or not self._ws_open.is_set():
            return
        try:
            self._ws.send(
                frame_binary(MSG_AUDIO, pcm16_bytes),
                opcode=websocket.ABNF.OPCODE_BINARY,
            )
            self._sent_audio += 1
        except Exception:
            pass

    def _on_cam_jpeg(self, jpeg_bytes: bytes) -> None:
        if self._ws is None or not self._ws_open.is_set():
            self._skipped_video += 1
            return
        try:
            self._ws.send(
                frame_binary(MSG_VIDEO, jpeg_bytes),
                opcode=websocket.ABNF.OPCODE_BINARY,
            )
            self._sent_video += 1
        except Exception:
            self._skipped_video += 1

    def _heartbeat_loop(self) -> None:
        last_stats_at = time.monotonic()
        while not self._stats_stop.wait(WS_PING_INTERVAL_S):
            if self._ws is None or not self._ws_open.is_set():
                continue
            now = time.monotonic()
            if now - self._last_pong_at > WS_PONG_TIMEOUT_S:
                self._emit("error", AttentionErrorEvent(
                    title="Connection Stalled",
                    message="No pong received within timeout window.",
                    detail=f"{now - self._last_pong_at:.1f}s since last pong",
                ))
            self._send_control({"action": "ping", "ts": time.monotonic() * 1000.0})
            if now - last_stats_at >= WS_STATS_INTERVAL_S:
                self._emit("stats", StatsEvent(
                    rtt_ms=self._last_rtt_ms,
                    sent_video=self._sent_video,
                    skipped_video=self._skipped_video,
                    sent_audio=self._sent_audio,
                    uptime_s=now - self._ws_opened_at if self._ws_opened_at else 0.0,
                ))
                last_stats_at = now


def _clamp01(v: float) -> float:
    if v != v:  # NaN
        return 0.0
    return max(0.0, min(1.0, v))


def _b64_to_int16(b64: str) -> np.ndarray:
    if not b64:
        return np.zeros(0, dtype=np.int16)
    raw = base64.b64decode(b64)
    return np.frombuffer(raw, dtype=np.int16).copy()


def _close_to_error(code: int, reason: str, was_clean: bool) -> Optional[AttentionErrorEvent]:
    if code == 1000:
        return None
    if code == 1008:
        return AttentionErrorEvent(
            title="Auth Failed",
            message="Server rejected the auth token.",
            detail=reason or f"close code {code}",
            code=code,
        )
    if code == 1013:
        return AttentionErrorEvent(
            title="Rate Limited",
            message="Throttled by server — try again shortly.",
            detail=reason or f"close code {code}",
            code=code,
        )
    if code == 1006:
        return AttentionErrorEvent(
            title="Connection Failed",
            message="Could not reach the server.",
            detail=f"The server may be down or unreachable. (close code {code})",
            code=code,
        )
    if not was_clean:
        return AttentionErrorEvent(
            title="Disconnected",
            message="Connection lost unexpectedly.",
            detail=f"code={code} reason={reason or 'none'}",
            code=code,
        )
    return None
