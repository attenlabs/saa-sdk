from __future__ import annotations

import base64
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

import numpy as np
import websocket

from .capture import (
    SEND_INTERVAL_SAMPLES,
    TARGET_AUDIO_RATE,
    CameraCapture,
    CameraConfig,
    MicCapture,
    MicConfig,
    _linear_downsample,
)
from .events import (
    AttentionErrorEvent,
    ConfigEvent,
    DisconnectedEvent,
    InterjectionEvent,
    InterruptEvent,
    PredictionEvent,
    ReconnectedEvent,
    ReconnectingEvent,
    StateEvent,
    StatsEvent,
    TurnFrame,
    TurnReadyEvent,
    VadEvent,
)
from .ws_protocol import MSG_AUDIO, MSG_VIDEO, frame_binary

DEFAULT_SERVER_URL = "https://broker.attentionlabs.ai"
BROKER_ALLOCATE_TIMEOUT_S = 5.0
DEFAULT_THRESHOLD = 0.7

WS_PING_INTERVAL_S = 5.0
WS_PONG_TIMEOUT_S = 15.0
WS_STATS_INTERVAL_S = 10.0
WS_HANDSHAKE_TIMEOUT_S = 10.0

RECONNECT_BASE_S = 0.5
RECONNECT_CAP_S = 20.0
# Close codes we never reconnect on (auth/protocol/policy — retrying won't help).
FATAL_CLOSE_CODES = frozenset({1000, 1002, 1003, 1007, 1008, 1009, 1010, 1015})

logger = logging.getLogger("saa")

Listener = Callable[..., Any]


def _append_query(url: str, **params: str) -> str:
    """Return *url* with *params* set in its query string (overwriting a
    same-named key), preserving other existing params (so a broker-returned URL
    that already carries a ticket keeps it). None-valued params are skipped."""
    parts = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    q.update({k: v for k, v in params.items() if v is not None})
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))


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
        server_profile: Optional[str] = None,
        auto_reconnect: bool = True,
    ):
        self.url = url or DEFAULT_SERVER_URL
        self.token = token
        self.video_config = video or CameraConfig()
        self.audio_config = audio or MicConfig()
        self.enable_audio = enable_audio
        self.enable_video = enable_video
        self.server_profile = server_profile
        self.auto_reconnect = auto_reconnect
        self.threshold = _clamp01(initial_threshold)

        self._listeners: dict[str, list[Listener]] = {}
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_open = threading.Event()
        self._ws_closed = threading.Event()

        self._handshake_done = threading.Event()
        self._close_info: dict = {}
        self._mic: Optional[MicCapture] = None
        self._cam: Optional[CameraCapture] = None
        self._stats_thread: Optional[threading.Thread] = None
        self._stats_stop = threading.Event()
        self._stall_emitted = False
        self._started = False

        # reconnect state
        self._stopping = False
        self._reconnecting = False
        self._reconnect_stop = threading.Event()
        self._reconnect_thread: Optional[threading.Thread] = None

        # resolved ws url + derived http origin (for the client_log fallback)
        self._resolved_ws_url: Optional[str] = None
        self._log_origin: Optional[str] = None

        # External-feed mode (enable_audio=False + feed_audio()): re-chunks
        # arbitrary-sized fed audio into the same 100 ms blocks the mic path
        # produces, so the server sees an identical cadence either way.
        self._feed_buffer = np.zeros(0, dtype=np.float32)
        self._feed_lock = threading.Lock()

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
    def on_interjection(self, func: Listener) -> Listener: return self._register("interjection", func)
    def on_error(self, func: Listener) -> Listener: return self._register("error", func)
    def on_disconnected(self, func: Listener) -> Listener: return self._register("disconnected", func)
    def on_reconnecting(self, func: Listener) -> Listener: return self._register("reconnecting", func)
    def on_reconnected(self, func: Listener) -> Listener: return self._register("reconnected", func)

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
        self._stopping = False
        self._reconnecting = False
        self._reconnect_stop.clear()
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

        # Set _stopping FIRST so any in-flight reconnect loop bails before
        # spawning a fresh socket, then interrupt its backoff sleep. Close any
        # in-flight ws so a mid-handshake attempt returns immediately, then join.
        self._stopping = True
        self._reconnect_stop.set()
        if self._reconnect_thread is not None:
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:
                    pass
            self._reconnect_thread.join(timeout=WS_HANDSHAKE_TIMEOUT_S + 1.0)
            self._reconnect_thread = None

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
        self._reconnecting = False
        with self._feed_lock:
            self._feed_buffer = np.zeros(0, dtype=np.float32)

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

    def send_client_log(self, entries: list) -> bool:
        """Ship a batch of client log entries to the server.

        When the WS is open, sends a ``client_log`` control frame. When it's
        closed (e.g. mid-reconnect), POSTs best-effort to the resolved origin's
        ``/client_log`` on a daemon thread — fire-and-forget, never blocks or
        raises. Returns True if dispatched (WS path) or queued (beacon path).
        """
        if not entries:
            return True  # nothing to ship, treated as success
        if self._send_control({"action": "client_log", "entries": entries}):
            return True
        return self._beacon_client_log(entries)

    def _beacon_client_log(self, entries: list) -> bool:
        origin = self._log_origin
        if not origin:
            return False
        url = origin + "/client_log"
        body = json.dumps({"entries": entries}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        def _post():
            try:
                req = urllib.request.Request(url, method="POST", headers=headers, data=body)
                urllib.request.urlopen(req, timeout=3.0).close()
            except Exception:
                pass  # best-effort

        threading.Thread(target=_post, daemon=True, name="saa-clientlog").start()
        return True

    # ── external feed (bring-your-own-capture) ────────────────────

    def feed_audio(self, audio: Any, *, sample_rate: int = TARGET_AUDIO_RATE) -> None:
        """Stream externally-captured audio instead of the SDK's own mic.

        Use this when another stack already owns the microphone — e.g. an
        ElevenLabs / OpenAI Realtime ``AudioInterface`` tap, or a Twilio media
        stream. Construct the client with ``enable_audio=False`` so the SDK
        never opens a mic, then call ``feed_audio`` for every captured chunk.

        Args:
            audio: PCM samples, mono, as ``bytes`` (int16 little-endian),
                ``np.int16``, or ``np.float32`` in [-1, 1]. Arbitrary length —
                re-chunked internally to the wire's 100 ms blocks.
            sample_rate: sample rate of ``audio``. Resampled to 16 kHz when it
                differs; default 16 kHz (no resample — the common case).

        Frames fed before the WebSocket is open (e.g. during a reconnect) are
        dropped, mirroring the mic path. Raises if the SDK is capturing its own
        mic (``enable_audio=True``) or has not been started.
        """
        if self.enable_audio:
            raise RuntimeError(
                "feed_audio() requires enable_audio=False — the SDK is "
                "capturing its own mic, so feeding would double the source"
            )
        if not self._started:
            raise RuntimeError("call start() before feed_audio()")

        samples = _to_float32_mono(audio)
        if samples.size == 0:
            return
        if sample_rate != TARGET_AUDIO_RATE:
            samples = _linear_downsample(samples, sample_rate, TARGET_AUDIO_RATE)

        with self._feed_lock:
            self._feed_buffer = np.concatenate([self._feed_buffer, samples])
            chunks: list[np.ndarray] = []
            while len(self._feed_buffer) >= SEND_INTERVAL_SAMPLES:
                chunks.append(self._feed_buffer[:SEND_INTERVAL_SAMPLES])
                self._feed_buffer = self._feed_buffer[SEND_INTERVAL_SAMPLES:]

        for chunk in chunks:
            pcm16 = np.clip(chunk * 32768.0, -32768, 32767).astype(np.int16)
            self._on_mic_pcm16(pcm16.tobytes())

    def feed_video(self, frame: Any) -> None:
        """Stream an externally-captured video frame instead of the SDK's own camera.

        Use this when another stack already owns the camera. Construct the
        client with ``enable_video=False`` so the SDK never opens a camera, then
        call ``feed_video`` for every frame.

        Args:
            frame: a pre-encoded JPEG as ``bytes``/``bytearray``/``memoryview``
                (sent as-is — the symmetric counterpart to the JS SDK's
                ``feedVideo(Blob | ArrayBuffer)``), or a raw image as an
                ``np.ndarray`` (H×W or H×W×C, BGR like OpenCV) which is
                JPEG-encoded with the client's ``CameraConfig.jpeg_quality``
                before sending.

        Frames fed before the WebSocket is open (e.g. during a reconnect) are
        dropped, mirroring the camera path. Raises if the SDK is capturing its
        own camera (``enable_video=True``) or has not been started.
        """
        if self.enable_video:
            raise RuntimeError(
                "feed_video() requires enable_video=False — the SDK is "
                "capturing its own camera, so feeding would double the source"
            )
        if not self._started:
            raise RuntimeError("call start() before feed_video()")

        if isinstance(frame, (bytes, bytearray, memoryview)):
            jpeg = bytes(frame)
            if not jpeg:
                return
        else:
            import cv2

            arr = np.asarray(frame)
            if arr.size == 0:
                return
            params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.video_config.jpeg_quality)]
            ok, buf = cv2.imencode(".jpg", arr, params)
            if not ok:
                return
            jpeg = buf.tobytes()
        self._on_cam_jpeg(jpeg)

    # ── WS ────────────────────────────────────────────────────────

    def _effective_server_profile(self) -> Optional[str]:
        """The server_profile this session requests, or None for the server
        default. Explicit ``server_profile=`` wins; otherwise ``enable_video=
        False`` selects ``"audio_only"``. ``"default"`` is the server's implicit
        profile, so it resolves to None — omitting the selector keeps legacy
        behavior byte-for-byte."""
        if self.server_profile is not None:
            prof = self.server_profile
        else:
            prof = None if self.enable_video else "audio_only"
        return prof if prof and prof != "default" else None

    def _resolve_ws_url(self) -> str:
        """Resolve self.url to a concrete wss://…/ws URL and remember it.

        Stores the resolved url + derived http origin (for the client_log
        fallback) before returning. Called once per connect, so reconnects
        re-resolve and pick a fresh least-loaded backend each time.
        """
        ws_url = self._resolve_ws_url_inner()
        self._resolved_ws_url = ws_url
        self._log_origin = _ws_url_to_origin(ws_url)
        return ws_url

    def _resolve_ws_url_inner(self) -> str:
        """Resolve self.url to a concrete wss://…/ws URL.

        - ws(s)://… is treated as a direct backend URL; the server_profile (if
          any) is appended as a query param the backend /ws reads.
        - http(s)://… is treated as a broker base URL; the broker
          bakes the selector into the wss URL it returns.

        Uses urllib (stdlib) — no new deps.
        """
        url = self.url
        profile = self._effective_server_profile()
        if url.startswith("ws://") or url.startswith("wss://"):
            if not profile:
                return url
            if self.server_profile is None:
                existing = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                if "server_profile" in existing:
                    return url
            return _append_query(url, server_profile=profile)
        allocate_url = url.rstrip("/") + "/allocate"
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if profile:
            body = json.dumps({"server_profile": profile}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            body = b""  # legacy: empty body, broker picks the default profile
        req = urllib.request.Request(
            allocate_url, method="POST", headers=headers, data=body,
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
        self._handshake_done.clear()
        self._close_info = {}
        self._sent_audio = 0
        self._sent_video = 0
        self._skipped_video = 0
        self._last_rtt_ms = None
        self._ws_opened_at = 0.0
        self._warmed_up = False
        self._stall_emitted = False

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

        # Poll the handshake so stop()/_reconnect_stop can abort an in-flight
        # connect instead of blocking the full handshake timeout.
        deadline = time.monotonic() + WS_HANDSHAKE_TIMEOUT_S
        done = False
        while time.monotonic() < deadline:
            if self._stopping or self._reconnect_stop.is_set():
                try:
                    self._ws.close()
                except Exception:
                    pass
                raise ConnectionError("WebSocket connect aborted by stop()")
            if self._handshake_done.wait(timeout=0.1):
                done = True
                break
        if not done:
            try:
                self._ws.close()
            except Exception:
                pass
            raise TimeoutError(
                f"WebSocket handshake timed out after {WS_HANDSHAKE_TIMEOUT_S}s (url={self.url})"
            )
        if not self._ws_open.is_set():
            # Handshake finished by closing rather than opening — report why.
            info = self._close_info
            raise ConnectionError(
                f"WebSocket closed during handshake: "
                f"code={info.get('code')} reason={info.get('reason') or 'none'}"
            )

    def _on_ws_open(self, ws) -> None:
        self._ws_opened_at = time.monotonic()
        self._last_pong_at = self._ws_opened_at
        self._stall_emitted = False
        self._ws_open.set()
        self._handshake_done.set()
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
        # code=0 / no-code normalizes to 1006 for retry classification.
        norm_code = code or 1006
        self._close_info = {"code": code, "reason": reason, "was_clean": was_clean}

        opened_mid_session = bool(self._ws_opened_at)
        # an unclean drop after the session was up
        # means audio/predictions/turns stop until we reconnect.
        if not was_clean and opened_mid_session:
            logger.warning(
                "[saa] websocket closed mid-session: code=%s reason=%s"
                " — predictions/turns stop until reconnect",
                code, reason or "none",
            )

        self._ws_closed.set()
        # Clear so the heartbeat guard and senders see a dead socket; set
        # _handshake_done so a blocked _open_ws_blocking waiter unblocks.
        self._ws_open.clear()
        self._handshake_done.set()

        # A socket that never opened (failed initial handshake or a failed
        # reconnect attempt) emits no lifecycle/error events — the JS `settled`
        # check does the same. The initial case raises out of start(); the
        # reconnect loop logs+retries on its own.
        if not opened_mid_session:
            return

        # Reconnect only for an opened mid-session socket that dropped on a
        # retriable code while we aren't stopping.
        will_reconnect = (
            self.auto_reconnect
            and not self._stopping
            and _is_retriable(norm_code)
        )

        self._emit("disconnected", DisconnectedEvent(
            code=code, reason=reason, was_clean=was_clean,
        ))

        # B8: suppress the scary error when we're about to reconnect — the
        # reconnecting/reconnected events tell that story instead. Also stay
        # silent while a reconnect loop is already running (a re-opened socket
        # that dropped again) so the loop solely owns the narrative.
        if not will_reconnect and not self._reconnecting:
            err = _close_to_error(code, reason, was_clean)
            if err is not None:
                self._emit("error", err)

        if will_reconnect:
            self._spawn_reconnect(norm_code)

    def _on_ws_error(self, ws, error) -> None:
        # The following _on_ws_close emits the user-facing error; we only
        # raise log visibility here, never double-fire an error event.
        logger.warning("ws error: %s", error)

    # ── reconnect ─────────────────────────────────────────────────

    def _spawn_reconnect(self, last_code: int) -> None:
        # Runs on the dying ws thread, so hand off to a fresh daemon thread.
        if self._reconnecting:
            return
        self._reconnecting = True
        self._reconnect_stop.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, args=(last_code,),
            daemon=True, name="saa-reconnect",
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self, last_code: int) -> None:
        attempt = 0
        try:
            while not self._stopping and not self._reconnect_stop.is_set():
                # Full-jitter backoff: uniform in [0, min(cap, base * 2**k)].
                ceiling = min(RECONNECT_CAP_S, RECONNECT_BASE_S * (2 ** attempt))
                delay = random.uniform(0.0, ceiling)
                self._emit("reconnecting", ReconnectingEvent(
                    attempt=attempt + 1, delay_s=delay, last_code=last_code,
                ))
                # Interruptible sleep — stop() sets _reconnect_stop.
                if self._reconnect_stop.wait(timeout=delay):
                    return
                if self._stopping:
                    return
                try:
                    self._open_ws_blocking()
                except Exception as e:
                    logger.warning("[saa] reconnect attempt %d failed: %s", attempt + 1, e)
                    attempt += 1
                    continue
                # stop() may have landed while this attempt was mid-handshake —
                # tear the fresh socket down rather than bring it up on a
                # stopped client (no heartbeat, no 'reconnected' narrative).
                if self._stopping or self._reconnect_stop.is_set():
                    try:
                        self._ws.close()
                    except Exception:
                        pass
                    return
                # Opened — mic/cam/heartbeat threads resume on the new socket.
                self._emit("reconnected", ReconnectedEvent(attempts=attempt + 1))
                return
        finally:
            self._reconnecting = False

    def _handle_msg(self, msg: dict) -> None:
        t = msg.get("type")

        if t == "pong":
            self._last_pong_at = time.monotonic()
            self._stall_emitted = False  # reset the stall latch on every pong
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

            source = msg.get("source") or ""
            self._emit("prediction", PredictionEvent(
                cls=int(cls),
                confidence=float(conf),
                source=source,
                num_faces=int(msg.get("num_faces") or 0),
                responding=bool(msg.get("responding", source == "ai_responding")),
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
            # quick latency check
            server_ts = msg.get("server_turn_ready_ts_ms")
            if isinstance(server_ts, (int, float)):
                transit_ms = time.time() * 1000.0 - float(server_ts)
                logger.info(
                    "[saa-timing] turn_ready transit (server→client) %.0fms"
                    " (clock-skew sensitive)", transit_ms,
                )
            raw_frames = msg.get("frames") or []
            frames = [
                TurnFrame(
                    ts_offset_s=float(f.get("ts_offset_s") or 0.0),
                    image_base64=str(f.get("image_base64") or ""),
                )
                for f in raw_frames
                if isinstance(f, dict) and f.get("image_base64")
            ]
            ctx = msg.get("context")
            self._emit("turn_ready", TurnReadyEvent(
                audio_pcm16=_b64_to_int16(b64),
                audio_base64=b64,
                duration_sec=float(msg.get("duration") or 0.0),
                frames=frames,
                context=str(ctx) if isinstance(ctx, str) else None,
            ))
        elif t == "started":
            self._emit("started")
            # `started` only means the model is loaded. Re-push the threshold
            # and re-apply mute here so a reconnected session restores its
            # state uniformly with the initial one (no separate resync path).
            self._send_control({"action": "set_threshold", "value": self.threshold})
            if self._muted:
                self._send_control({"action": "mute"})
        elif t == "warmup_complete":
            if not self._warmed_up:
                self._warmed_up = True
                self._emit("warmup_complete")
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
        elif t == "interjection":
            b64 = msg.get("audio_base64") or ""
            self._emit("interjection", InterjectionEvent(
                reason=msg.get("reason") or "",
                audio_pcm16=_b64_to_int16(b64),
                audio_base64=b64,
                duration_sec=float(msg.get("duration_s") or 0.0),
            ))
        elif t == "error":
            self._emit("error", AttentionErrorEvent(
                title="Server Error",
                message=msg.get("message") or "",
                detail=msg.get("detail"),
                kind="server",
            ))

    def _send_control(self, data: dict) -> bool:
        if self._ws is None or not self._ws_open.is_set():
            return False
        try:
            self._ws.send(json.dumps(data))
            return True
        except Exception:
            return False

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
                # Emit once per stall episode (latch resets on the next pong),
                # then force-close so reconnect/teardown takes over and we stop
                # pinging a half-open socket.
                if not self._stall_emitted:
                    self._stall_emitted = True
                    self._emit("error", AttentionErrorEvent(
                        title="Connection Stalled",
                        message="No pong received within timeout window.",
                        detail=f"{now - self._last_pong_at:.1f}s since last pong",
                        kind="transport",
                        retriable=True,
                    ))
                    try:
                        self._ws.close()
                    except Exception:
                        pass
                continue
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


def _to_float32_mono(audio: Any) -> np.ndarray:
    """Normalize fed audio (bytes / int16 / float ndarray) to float32 mono [-1, 1]."""
    if isinstance(audio, (bytes, bytearray, memoryview)):
        return np.frombuffer(bytes(audio), dtype=np.int16).astype(np.float32) / 32768.0
    arr = np.asarray(audio)
    if arr.ndim > 1:
        # interleaved frames → first channel
        arr = arr[:, 0]
    arr = np.ascontiguousarray(arr.reshape(-1))
    if arr.dtype == np.int16:
        return arr.astype(np.float32) / 32768.0
    if np.issubdtype(arr.dtype, np.floating):
        return arr.astype(np.float32)
    # any other int width — assume full-scale signed, normalize by int16 range
    return arr.astype(np.float32) / 32768.0


def _is_retriable(code: int) -> bool:
    """Retriable = close code NOT in the fatal set (auth/protocol/policy)."""
    return code not in FATAL_CLOSE_CODES


def _ws_url_to_origin(ws_url: str) -> Optional[str]:
    """Derive the http origin for the client_log fallback from a resolved ws
    url: wss->https, ws->http, path /client_log. Returns scheme://host[:port]."""
    try:
        parts = urllib.parse.urlsplit(ws_url)
    except Exception:
        return None
    scheme = {"wss": "https", "ws": "http"}.get(parts.scheme)
    if not scheme or not parts.netloc:
        return None
    return f"{scheme}://{parts.netloc}"


def _close_to_error(code: int, reason: str, was_clean: bool) -> Optional[AttentionErrorEvent]:
    if code == 1000:
        return None
    if code == 1008:
        return AttentionErrorEvent(
            title="Auth Failed",
            message="Server rejected the auth token.",
            detail=reason or f"close code {code}",
            code=code,
            kind="auth",
            retriable=False,
        )
    if code == 1013:
        return AttentionErrorEvent(
            title="Rate Limited",
            message="Throttled by server — try again shortly.",
            detail=reason or f"close code {code}",
            code=code,
            kind="rate_limit",
            retriable=True,
        )
    if code in (1006, 0):
        return AttentionErrorEvent(
            title="Connection Failed",
            message="Could not reach the server.",
            detail=(
                f"The server may be down or unreachable."
                f" (close code {code}, reason={reason or 'none'})"
            ),
            code=code,
            kind="transport",
            retriable=True,
        )
    if not was_clean:
        return AttentionErrorEvent(
            title="Disconnected",
            message="Connection lost unexpectedly.",
            detail=f"code={code} reason={reason or 'none'}",
            code=code,
            kind="transport",
            retriable=True,
        )
    return None
