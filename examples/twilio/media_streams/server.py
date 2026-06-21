"""SAA gating for Twilio Media Streams, production-grade reference adapter.

Flow per call:

    Caller (PSTN) → Twilio → /voice (TwiML) → /twilio (WebSocket)
                                                  ↓
                            inbound µ-law @ 8 kHz │ 20 ms
                                                  ↓
                                          audio.py decode + 16 kHz upsample
                                                  ↓
                                          AttentionClient (signaling-only)
                                                  ↓
                                          turn_ready (PCM16 @ 16 kHz)
                                                  ↓
                                          Bridge.on_speech()
                                          (your STT / LLM / TTS lives here)
                                                  ↓
                                          Bridge.outbound_pcm16_16k
                                                  ↓
                                          audio.py 8 kHz downsample + encode
                                                  ↓
                            outbound µ-law @ 8 kHz │ 20 ms
                                                  ↓
                                              Twilio → Caller

What this file does:

* serves ``/voice`` (POST) returning TwiML that opens a Media Stream
* serves ``/voice/outbound`` (POST) for outbound calls created via the Twilio API
* serves ``/twilio-status`` (POST) for Twilio call-status callbacks
  (initiated / ringing / answered / completed)
* serves ``/twilio`` (WebSocket), bidirectional Media Streams handler
* serves ``/health`` (GET) and ``/ready`` (GET) for liveness / readiness probes
* serves ``/stats`` (GET) returning Prometheus-shaped aggregate counters
  (calls, audio bytes, SAA errors, barge-ins)
* validates the ``X-Twilio-Signature`` header when ``TWILIO_AUTH_TOKEN`` is set
* gates every call through SAA using ``enable_audio=False`` / ``enable_video=False``
  plus the SDK's public ``feed_audio()`` API (no local mic / cam are ever opened)
* paces outbound TTS at Twilio's recommended ~20 ms cadence so barge-in
  latency stays under a frame
* propagates SAA state to a barge-in event so your bridge can flush its
  TTS buffer when the caller talks back

What you need to wire yourself:

* a :class:`bridge.Bridge` implementation that actually answers the call
  (the default :class:`bridge.LoggingBridge` only logs)

References:

* Twilio Media Streams protocol: https://www.twilio.com/docs/voice/media-streams
* Twilio TwiML <Stream>:          https://www.twilio.com/docs/voice/twiml/stream
* Twilio webhook security:        https://www.twilio.com/docs/usage/security
* SAA Python SDK reference:       https://attentionlabs.ai/docs/python/reference/
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse, PlainTextResponse, Response
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "fastapi is required: pip install -r examples/twilio/requirements.txt"
    ) from exc

import numpy as np

from saa import AttentionClient

from audio import (
    chunk_ulaw_20ms,
    pcm16_16k_to_twilio_payload,
    pcm16_to_ulaw,
    downsample_16k_to_8k,
    twilio_payload_to_pcm16_16k,
)
from bridge import Bridge, CallContext, CallSession, LoggingBridge
from twiml import twiml_for_stream


log = logging.getLogger("saa.twilio")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# ── Config ───────────────────────────────────────────────────────────────


DEFAULT_THRESHOLD = float(os.environ.get("SAA_THRESHOLD", "0.7"))
DEFAULT_TOKEN = os.environ.get("SAA_API_KEY", "")
DEFAULT_SAA_URL = os.environ.get("ATTENLABS_URL")  # None → cloud default
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
PUBLIC_HOSTNAME = os.environ.get("PUBLIC_HOSTNAME", "")  # set when behind ngrok / a load balancer
OUTBOUND_PACE_SECONDS = float(os.environ.get("OUTBOUND_PACE_SECONDS", "0.02"))
OUTBOUND_QUEUE_MAX = int(os.environ.get("OUTBOUND_QUEUE_MAX", "512"))
# How long the adapter waits for SAA's `started` (warmup-complete) signal
# before it begins forwarding caller PCM. Without this gate, the first
# ~100 ms of every call lands in the cloud's warmup window and gets
# dropped, callers hear themselves say "hel-" before the gate engages.
SAA_WAIT_READY_SECONDS = float(os.environ.get("SAA_WAIT_READY_SECONDS", "3.0"))
# Trailing-silence tail kept on mark_responding=False so a sub-frame
# echo of the last TTS byte doesn't re-trigger SAA. Counted from the
# moment the outbound queue drains.
RESPONDING_TAIL_SECONDS = float(os.environ.get("RESPONDING_TAIL_SECONDS", "0.25"))

# Twilio sends 20 ms (160 samples = 320 PCM16 bytes) frames @ 8 kHz.
# After upsample to 16 kHz, that becomes 640 bytes per frame. SAA expects
# 100 ms (3200 PCM16 bytes) frames per the SDK's WebSocket protocol
# (5 Twilio frames per SAA frame).
SAA_FRAME_BYTES = 3200  # 100 ms @ 16 kHz, int16 mono

# Twilio recommends sending media events at the audio's playback cadence
# so the carrier's jitter buffer stays shallow and barge-in remains snappy.
# 20 ms = 160 µ-law bytes per frame, which is also the Twilio inbound frame
# size, sender-side symmetry simplifies reasoning.
OUTBOUND_ULAW_FRAME_BYTES = 160


# ── BridgeFactory: how the app constructs a Bridge per call ──────────────


BridgeFactory = Callable[[], Awaitable[Bridge]] | Callable[[], Bridge]


async def _default_bridge_factory() -> Bridge:
    return LoggingBridge()


_bridge_factory: BridgeFactory = _default_bridge_factory


def set_bridge_factory(factory: BridgeFactory) -> None:
    """Override the per-call bridge factory.

    Call this at startup if you've wired up your own STT/LLM/TTS bridge::

        from server import app, set_bridge_factory
        from my_bridges import OpenAIRealtimeBridge
        set_bridge_factory(lambda: OpenAIRealtimeBridge(api_key=...))
    """
    global _bridge_factory
    _bridge_factory = factory


async def _make_bridge() -> Bridge:
    result = _bridge_factory()  # type: ignore[operator]
    if asyncio.iscoroutine(result):
        return await result
    return result  # type: ignore[return-value]


# ── App ──────────────────────────────────────────────────────────────────


app = FastAPI(title="saa × twilio adapter", version="1.0.0")


@dataclass
class _CallStats:
    audio_bytes_in: int = 0
    audio_bytes_out: int = 0
    turn_ready_count: int = 0
    barge_ins: int = 0
    saa_errors: int = 0
    saa_reconnects: int = 0
    started_at: float = field(default_factory=time.monotonic)


class _TwilioCallSession:
    """Concrete :class:`bridge.CallSession` bound to one Twilio WebSocket.

    The bridge holds a reference; methods are safe to call from anywhere on
    the loop and are no-ops once the WebSocket has closed. Outbound bytes
    take the same path as :meth:`bridge.outbound_pcm16_16k` enqueue, the
    paced sender drains them on a 20 ms tick.

    SAA controls (``mark_responding`` / ``mute`` / ``unmute`` /
    ``set_threshold``) tunnel directly into the per-call ``AttentionClient``
    instance so bridges don't need to hold their own SDK handle.
    """

    def __init__(
        self,
        ws: WebSocket,
        call_sid: str,
        stream_sid: str,
        outbound_queue: "asyncio.Queue[Optional[bytes]]",
        twilio_client_factory: Callable[[], Any],
        saa: "AttentionClient",
    ) -> None:
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        self._ws = ws
        self._outbound_queue = outbound_queue
        self._twilio_client_factory = twilio_client_factory
        self._saa = saa
        self._closed = False
        self._lock = asyncio.Lock()
        self._responding = False

    @property
    def is_open(self) -> bool:
        return not self._closed

    def _mark_closed(self) -> None:
        self._closed = True

    async def send_audio(
        self, pcm16_16k: Union[bytes, bytearray, np.ndarray]
    ) -> None:
        if self._closed:
            return
        if isinstance(pcm16_16k, np.ndarray):
            data = pcm16_16k.astype(np.int16, copy=False).tobytes()
        else:
            data = bytes(pcm16_16k)
        if not data:
            return
        # Enqueue rather than send-direct: keeps the 20 ms pacing invariant
        # whether the bridge enqueues or calls send_audio directly.
        try:
            self._outbound_queue.put_nowait(data)
        except asyncio.QueueFull:
            # Stale TTS, likely already past its barge-in window. Drop the
            # frame rather than block; observability lives on the queue.
            log.warning(
                "[session %s] outbound queue full, dropping %d bytes",
                self.stream_sid, len(data),
            )

    async def clear_playback(self) -> None:
        """Flush both our local buffer AND Twilio's outbound playback queue.

        Twilio's ``clear`` event drops every queued ``media`` frame on the
        carrier side. Pair it with draining our pending bytes so any
        in-flight TTS the bridge already enqueued doesn't sneak through
        after a barge-in.
        """
        if self._closed:
            return
        async with self._lock:
            # Drain anything queued locally (preserving the terminating None).
            saw_sentinel = False
            try:
                while True:
                    item = self._outbound_queue.get_nowait()
                    if item is None:
                        saw_sentinel = True
                    self._outbound_queue.task_done()
            except asyncio.QueueEmpty:
                pass
            if saw_sentinel:
                # Put the close sentinel back so the sender still terminates.
                await self._outbound_queue.put(None)
            await self._send_json({
                "event": "clear",
                "streamSid": self.stream_sid,
            })

    async def send_mark(self, name: str) -> None:
        if self._closed:
            return
        await self._send_json({
            "event": "mark",
            "streamSid": self.stream_sid,
            "mark": {"name": name},
        })

    async def hangup(self) -> None:
        """Programmatically end the call.

        Closes the Twilio Media Stream WebSocket cleanly. Twilio terminates
        the PSTN leg on its side as soon as we drop the stream. If the
        Twilio REST client is available and credentials are set, also
        issues a REST hangup so completed-status callbacks fire even if
        the WS close is racy.
        """
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):
            await self._ws.close(code=1000)
        # Best-effort REST hangup. Failure is logged but not raised, the
        # WebSocket close above already ended the media leg.
        client = self._twilio_client_factory()
        if client is not None and self.call_sid:
            try:
                await asyncio.to_thread(
                    lambda: client.calls(self.call_sid).update(status="completed")
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    "[session %s] REST hangup failed (WS already closed)",
                    self.stream_sid, exc_info=True,
                )

    async def mark_responding(self, responding: bool) -> None:
        """Signal SAA that the agent is / is not currently speaking.

        The SAA server suppresses predictions while responding==True so the
        agent's own TTS echo (coming back through the carrier loop) can't
        trigger a second ``turn_ready``. The adapter calls this
        automatically when bytes start / stop flowing through the outbound
        queue, but bridges can also call it explicitly for finer control
        (e.g., during a long async LLM round trip before TTS audio appears).
        """
        if self._closed:
            return
        self._responding = bool(responding)
        with suppress(Exception):
            await asyncio.to_thread(self._saa.mark_responding, bool(responding))

    async def mute(self) -> None:
        """Privacy mute, drop caller PCM at the relay before it reaches SAA.

        Useful for compliance: after a caller answers a recording-disclosure
        prompt with "no", mute the SAA path so the cloud model never sees
        the call audio. The bridge can still hear it locally; this only
        gates the upstream feed.
        """
        if self._closed:
            return
        with suppress(Exception):
            await asyncio.to_thread(self._saa.mute)

    async def unmute(self) -> None:
        if self._closed:
            return
        with suppress(Exception):
            await asyncio.to_thread(self._saa.unmute)

    async def set_threshold(self, value: float) -> None:
        """Retune the device-class confidence threshold mid-call.

        Effective range 0..1. Higher = SAA is stricter about what counts as
        device-directed (fewer false turn_ready firings, more missed
        utterances). Lower = more permissive. Reasonable telephony band
        is 0.6 – 0.8; default is whatever the relay started with.
        """
        if self._closed:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        v = max(0.0, min(1.0, v))
        with suppress(Exception):
            await asyncio.to_thread(self._saa.set_threshold, v)

    async def _send_json(self, payload: dict) -> None:
        try:
            await self._ws.send_text(json.dumps(payload))
        except Exception:  # noqa: BLE001
            self._closed = True


# ── HTTP routes ──────────────────────────────────────────────────────────


# Aggregate counters across every call so /stats can serve a single
# snapshot suitable for Prometheus/CloudWatch scraping. Per-call data
# lives on _CallStats; the aggregate is updated when calls end.
#
# threading.Lock, not asyncio.Lock: the latter is bound to the event loop
# it was created on, and FastAPI's TestClient creates a new loop per
# test. A threading.Lock works across loops and is plenty for counter
# updates (we hold it for nanoseconds).
_AGGREGATE_STATS: dict[str, float] = {
    "calls_total": 0.0,
    "calls_active": 0.0,
    "audio_bytes_in_total": 0.0,
    "audio_bytes_out_total": 0.0,
    "turn_ready_total": 0.0,
    "barge_ins_total": 0.0,
    "saa_errors_total": 0.0,
    "saa_reconnects_total": 0.0,
    "call_duration_seconds_total": 0.0,
}
_AGGREGATE_LOCK = threading.Lock()


@app.get("/health")
async def health() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.get("/stats")
async def stats_snapshot() -> JSONResponse:
    """Aggregate per-process counters, calls, audio, SAA errors, barge-ins.

    Mirrors the keys you'd pipe into Prometheus: every value is a float
    counter except ``calls_active`` which is a gauge. The snapshot is
    process-local; multi-worker deployments should add a label aggregator
    in front (e.g., a Prom exporter), or scrape each worker pod.
    """
    with _AGGREGATE_LOCK:
        snapshot = dict(_AGGREGATE_STATS)
    return JSONResponse(snapshot)


def _validate_twilio_signature(request: Request, body: bytes) -> None:
    """Validate the X-Twilio-Signature header against the request body.

    No-op when ``TWILIO_AUTH_TOKEN`` is unset (development convenience).
    Strict-mode operators should always set the auth token, a single
    unsigned POST to ``/voice`` is enough for someone to redirect your
    callers to a stream URL of their choice.

    Reference: https://www.twilio.com/docs/usage/security#validating-requests
    """
    if not TWILIO_AUTH_TOKEN:
        return
    try:
        from twilio.request_validator import RequestValidator
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(
            status_code=500,
            detail="twilio package required for signature validation: pip install twilio",
        ) from exc

    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    signature = request.headers.get("x-twilio-signature", "")
    if not signature:
        raise HTTPException(status_code=403, detail="missing X-Twilio-Signature")
    # Twilio computes the signature against the public URL Twilio dialed,
    # not whatever scheme/host FastAPI sees behind a proxy. Operators set
    # PUBLIC_HOSTNAME to the externally-visible host (e.g., ngrok URL).
    if PUBLIC_HOSTNAME:
        url = f"https://{PUBLIC_HOSTNAME}{request.url.path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
    else:
        url = str(request.url)

    content_type = request.headers.get("content-type", "")
    params: dict[str, str] = {}
    if content_type.startswith("application/x-www-form-urlencoded"):
        from urllib.parse import parse_qsl

        params = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    elif content_type.startswith("application/json"):
        # Twilio's signature for JSON bodies hashes the raw body bytes
        # appended to the URL (see signature spec). RequestValidator's
        # ``validate`` path doesn't cover JSON; fall back to the manual
        # comparison used by the official twilio-python tests.
        from hashlib import sha256
        body_hash = sha256(body).hexdigest()
        if not validator.validate(url, body_hash, signature):
            raise HTTPException(status_code=403, detail="invalid Twilio signature")
        return
    if not validator.validate(url, params, signature):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")


def _twilio_client():
    """Return a cached Twilio REST client, or ``None`` when creds are missing."""
    if not TWILIO_AUTH_TOKEN:
        return None
    account = os.environ.get("TWILIO_ACCOUNT_SID", "")
    if not account:
        return None
    cached = getattr(_twilio_client, "_client", None)
    if cached is not None:
        return cached
    try:
        from twilio.rest import Client
    except ImportError:  # pragma: no cover
        return None
    client = Client(account, TWILIO_AUTH_TOKEN)
    _twilio_client._client = client  # type: ignore[attr-defined]
    return client


def _stream_url_for(request: Request) -> str:
    """Compute the wss:// URL Twilio should dial for this server."""
    if PUBLIC_HOSTNAME:
        return f"wss://{PUBLIC_HOSTNAME}/twilio"
    base = str(request.base_url).rstrip("/")
    return base.replace("http://", "wss://").replace("https://", "wss://") + "/twilio"


@app.get("/ready")
async def ready() -> Response:
    """Readiness probe, 200 once the SAA token is set, 503 otherwise."""
    if not DEFAULT_TOKEN:
        return JSONResponse(
            status_code=503,
            content={"status": "SAA_API_KEY unset"},
        )
    return PlainTextResponse("ready")


@app.post("/voice")
async def voice_inbound(request: Request) -> Response:
    """Twilio inbound voice webhook. Returns TwiML pointing at /twilio."""
    body = await request.body()
    _validate_twilio_signature(request, body)
    twiml = twiml_for_stream(_stream_url_for(request))
    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/outbound")
async def voice_outbound(request: Request) -> Response:
    """TwiML for outbound calls created via the Twilio REST API.

    Twilio fetches this URL once the called party answers; the response
    decides what happens next. We route straight into the Media Stream.
    """
    body = await request.body()
    _validate_twilio_signature(request, body)
    twiml = twiml_for_stream(
        _stream_url_for(request),
        greeting=os.environ.get("OUTBOUND_GREETING", "") or None,
    )
    return Response(content=twiml, media_type="application/xml")


@app.post("/twilio-status")
async def twilio_status(request: Request) -> Response:
    """Status callback endpoint used by ``outbound.py``.

    Twilio POSTs ``initiated`` / ``ringing`` / ``answered`` / ``completed``
    here for outbound calls. We log them at INFO so operators can correlate
    call SIDs with their Twilio console; production stacks typically also
    persist these into the call-events table that their billing or QA
    pipeline consumes.
    """
    body = await request.body()
    _validate_twilio_signature(request, body)
    from urllib.parse import parse_qsl

    params = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    call_sid = params.get("CallSid", "")
    status = params.get("CallStatus", "")
    duration = params.get("CallDuration", "")
    log.info(
        "[twilio] status callback: call=%s status=%s duration=%s",
        call_sid or "?", status or "?", duration or "-",
    )
    return PlainTextResponse("ok")


# ── WebSocket handler ────────────────────────────────────────────────────


@app.websocket("/twilio")
async def twilio_media_stream(ws: WebSocket) -> None:
    """Bidirectional Twilio Media Streams handler with SAA gating per call.

    Per Twilio's reference: a single call traverses ``connected`` →
    ``start`` → N × (``media`` | ``dtmf`` | ``mark``) → ``stop``. We map
    those onto the bridge/session API so every call gets one ``open``,
    one ``close``, and a paced 20 ms outbound stream.
    """
    # Twilio's client requires the "audio.twilio.com" subprotocol to be
    # negotiated on accept. Skipping it leads to silent disconnects.
    # Real Twilio Media Streams does NOT request a subprotocol; echoing one it did
    # not offer makes its client drop the connection. Only echo if actually offered.
    _offered = ws.headers.get("sec-websocket-protocol", "") or ""
    if "audio.twilio.com" in _offered:
        await ws.accept(subprotocol="audio.twilio.com")
    else:
        await ws.accept()

    if not DEFAULT_TOKEN:
        log.error("SAA_API_KEY not set, closing call.")
        await ws.close(code=1011)
        return

    conn_id = uuid.uuid4().hex[:8]
    loop = asyncio.get_running_loop()
    bridge = await _make_bridge()
    stats = _CallStats()
    with _AGGREGATE_LOCK:
        _AGGREGATE_STATS["calls_total"] += 1
        _AGGREGATE_STATS["calls_active"] += 1

    # AttentionClient is constructed with enable_audio=False / enable_video=False
    # so the SDK never opens the host's mic/camera. Phone audio reaches the
    # cloud exclusively via feed_audio() below — the supported pattern for
    # external-capture adapters like this Twilio relay.
    saa = AttentionClient(
        token=DEFAULT_TOKEN,
        url=DEFAULT_SAA_URL,
        enable_audio=False,    # we inject Twilio audio ourselves via feed_audio()
        enable_video=False,    # phone calls have no video track
        initial_threshold=DEFAULT_THRESHOLD,
    )

    # Bounded so a misbehaving bridge can't OOM the relay by enqueuing
    # TTS faster than Twilio can play it. Drops are logged in the session.
    outbound_queue: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue(
        maxsize=OUTBOUND_QUEUE_MAX,
    )
    # Some bridges (LoggingBridge, RecordingBridge in the suite) bring
    # their own queue. Use the bridge's if present, otherwise inject ours
    # so the sender has somewhere to read from.
    if getattr(bridge, "outbound_pcm16_16k", None) is None:
        bridge.outbound_pcm16_16k = outbound_queue
    else:
        outbound_queue = bridge.outbound_pcm16_16k  # type: ignore[assignment]

    # Helper for the SAA SDK's threaded callbacks: dispatch to the FastAPI
    # loop and swallow exceptions so a misbehaving bridge can't deadlock
    # the SAA receive thread.
    def _dispatch(coro):
        try:
            return asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            return None  # loop already closed

    @saa.on_turn_ready
    def _on_turn_ready(event):
        stats.turn_ready_count += 1
        _dispatch(bridge.on_speech(event.audio_pcm16, event.duration_sec))

    @saa.on_state
    def _on_saa_state(event):
        # SAA's "sending" state == utterance complete, model is forwarding
        # to the bridge. Treat the leading edge of a new "sending" as an
        # implicit barge-in signal so the bridge can flush in-flight TTS.
        if event.state == "sending":
            stats.barge_ins += 1
            fut = _dispatch(bridge.on_user_speech_started())
            if fut is not None:
                with suppress(Exception):
                    fut.result(timeout=0.05)  # ensure clear() lands before TTS resumes

    @saa.on_prediction
    def _on_saa_prediction(event):
        # Pass through to the bridge so adaptive bridges can read
        # per-frame confidence + source labels. No-op default on
        # LoggingBridge.
        _dispatch(bridge.on_saa_prediction(event))

    @saa.on_vad
    def _on_saa_vad(event):
        _dispatch(bridge.on_saa_vad(event))

    @saa.on_warmup_complete
    def _on_saa_warmup():
        _dispatch(bridge.on_saa_warmup_complete())

    @saa.on_stats
    def _on_saa_stats(event):
        _dispatch(bridge.on_saa_stats(event))

    @saa.on_error
    def _on_saa_error(event):
        stats.saa_errors += 1
        log.warning(
            "[saa %s] %s: %s (%s)",
            conn_id, event.title, event.message, event.detail or "",
        )
        # Hard, non-recoverable failures (auth, policy violation) should
        # tear down the call rather than wait for Twilio's stream to time
        # out, the user is paying carrier minutes for silence otherwise.
        if event.code in (1008, 4001):
            _dispatch(_force_disconnect("saa-auth-failed"))

    @saa.on_disconnected
    def _on_saa_disconnected(event):
        stats.saa_reconnects += 1
        log.info(
            "[saa %s] disconnected: code=%s reason=%s clean=%s",
            conn_id, event.code, event.reason or "-", event.was_clean,
        )

    # threading.Event set by on_started (and on_warmup_complete) so the
    # "start" handler can block until SAA's model is loaded before forwarding
    # caller audio. Using a threading.Event (not asyncio) lets us wait via
    # asyncio.to_thread without needing a running loop in the callback thread.
    ready_event = threading.Event()

    @saa.on_started
    def _on_saa_started():
        ready_event.set()

    @saa.on_warmup_complete
    def _on_saa_warmup_complete_ready():
        ready_event.set()

    # Buffer that turns Twilio's 20 ms inbound frames into 100 ms SAA frames.
    saa_buffer = bytearray()
    stream_sid: Optional[str] = None
    call_ctx: Optional[CallContext] = None
    session: Optional[_TwilioCallSession] = None
    saa_started = False
    sender_task: Optional[asyncio.Task] = None
    hangup_dispatched = False

    async def _force_disconnect(reason: str) -> None:
        nonlocal hangup_dispatched
        if hangup_dispatched:
            return
        hangup_dispatched = True
        log.info("[twilio %s] force disconnect: %s", conn_id, reason)
        with suppress(Exception):
            await ws.close(code=1011)

    async def _dispatch_hangup_once() -> None:
        nonlocal hangup_dispatched
        if hangup_dispatched:
            return
        hangup_dispatched = True
        try:
            await bridge.on_caller_hangup()
        except Exception:  # noqa: BLE001
            log.exception("[bridge %s] on_caller_hangup raised", conn_id)

    async def _paced_outbound_sender() -> None:
        """Drain ``outbound_queue`` and ship 20 ms µ-law frames to Twilio.

        Pacing matches Twilio's playback rate, frames go out every
        ``OUTBOUND_PACE_SECONDS``. The sender also drives the
        ``mark_responding`` SAA control: it auto-asserts ``True`` the
        instant bytes start flowing and lowers it back to ``False`` after
        ``RESPONDING_TAIL_SECONDS`` of silence. This is the single most
        important SDK feature for telephony, without it the agent's own
        TTS bleeds back through the carrier and SAA re-fires
        ``turn_ready`` on the echo (feedback loop). The bridge can also
        call ``session.mark_responding`` explicitly if it needs finer
        control during a long pre-TTS LLM round trip.

        Anything still pending when the call ends is dropped (carrier
        already gone). The sender terminates cleanly on a ``None``
        sentinel or when ``send_text`` raises.
        """
        # Tail check cadence, we wake every PACE so the cancel signal
        # from the call cleanup propagates quickly. Production telephony
        # uses a fixed 20 ms tick and ~250 ms responding tail.
        tail_check = min(OUTBOUND_PACE_SECONDS * 4, 0.05)
        pending = bytearray()  # µ-law bytes still to ship
        next_tick = time.monotonic()
        responding = False
        last_send_at = time.monotonic()
        while True:
            if not pending:
                # Drop ``mark_responding`` back to False once the queue has
                # been idle for RESPONDING_TAIL_SECONDS. The tail keeps a
                # sub-frame echo of the last TTS byte from re-arming SAA.
                if (
                    responding
                    and time.monotonic() - last_send_at >= RESPONDING_TAIL_SECONDS
                ):
                    responding = False
                    if session is not None:
                        await session.mark_responding(False)
                try:
                    chunk = await asyncio.wait_for(
                        outbound_queue.get(), timeout=tail_check,
                    )
                except asyncio.TimeoutError:
                    continue
                if chunk is None:
                    if responding and session is not None:
                        await session.mark_responding(False)
                    return
                if stream_sid is None or session is None or not session.is_open:
                    continue
                # First byte after silence → mark_responding(True). Done
                # before the byte goes out so SAA suppression covers the
                # whole TTS, including any one-frame race with the cloud.
                if not responding:
                    responding = True
                    await session.mark_responding(True)
                # PCM16 16 kHz → PCM16 8 kHz → µ-law @ 8 kHz.
                pcm16_16k = np.frombuffer(chunk, dtype=np.int16)
                ulaw = pcm16_to_ulaw(downsample_16k_to_8k(pcm16_16k))
                pending.extend(ulaw)
                stats.audio_bytes_out += len(chunk)
            # Ship one 20 ms frame per tick to keep the Twilio buffer shallow.
            frame = bytes(pending[:OUTBOUND_ULAW_FRAME_BYTES])
            del pending[:OUTBOUND_ULAW_FRAME_BYTES]
            payload = base64.b64encode(frame).decode("ascii")
            try:
                await ws.send_text(json.dumps({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": payload},
                }))
            except Exception:  # noqa: BLE001
                return
            last_send_at = time.monotonic()
            next_tick += OUTBOUND_PACE_SECONDS
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            else:
                # We fell behind, reset the wall-clock baseline so we
                # don't burst-send to catch up (which would defeat the
                # purpose of pacing).
                next_tick = time.monotonic()

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("[twilio %s] non-JSON frame ignored", conn_id)
                continue
            event = msg.get("event")

            if event == "connected":
                # First message after handshake. No state to set up, Twilio
                # is just confirming the WebSocket is alive.
                log.debug("[twilio %s] connected: protocol=%s version=%s",
                          conn_id, msg.get("protocol"), msg.get("version"))

            elif event == "start":
                start = msg.get("start") or {}
                stream_sid = start.get("streamSid") or ""
                custom = start.get("customParameters") or {}
                call_ctx = CallContext(
                    call_sid=start.get("callSid", ""),
                    stream_sid=stream_sid,
                    account_sid=start.get("accountSid", ""),
                    from_number=custom.get("From", ""),
                    to_number=custom.get("To", ""),
                    direction=custom.get("Direction", "inbound"),
                    custom_parameters=custom,
                )
                session = _TwilioCallSession(
                    ws=ws,
                    call_sid=call_ctx.call_sid,
                    stream_sid=stream_sid,
                    outbound_queue=outbound_queue,
                    twilio_client_factory=_twilio_client,
                    saa=saa,
                )
                log.info(
                    "[twilio %s] start: stream=%s call=%s from=%s to=%s",
                    conn_id, stream_sid, call_ctx.call_sid,
                    call_ctx.from_number or "-", call_ctx.to_number or "-",
                )
                # SAA WS open + bridge open + outbound sender all start now,
                # so we don't waste cloud quota on a call we never picked up.
                # saa.start() opens the WebSocket on a daemon thread, so
                # the call returns almost immediately; keep it on the loop.
                saa.start()
                saa_started = True
                # Block briefly for SAA's `started` signal (set by the
                # on_started / on_warmup_complete handlers above) so the first
                # 100 ms of caller audio actually hits a warm model instead of
                # landing in the cloud's warmup window. ready_event.wait()
                # blocks on a threading.Event so it must go through
                # asyncio.to_thread to avoid stalling the event loop.
                # Falling through on timeout is intentional: a slow cloud
                # is preferable to dropping the call.
                ready_ok = await asyncio.to_thread(
                    ready_event.wait, SAA_WAIT_READY_SECONDS,
                )
                if not ready_ok:
                    log.warning(
                        "[saa %s] warmup did not complete in %.1fs, forwarding audio anyway",
                        conn_id, SAA_WAIT_READY_SECONDS,
                    )
                # The Bridge protocol now takes a session, but the v0
                # shape took only ctx. Detect via signature so older
                # bridges keep working unchanged.
                import inspect
                try:
                    sig = inspect.signature(bridge.open)
                    if len(sig.parameters) >= 2:
                        await bridge.open(call_ctx, session)
                    else:
                        await bridge.open(call_ctx)  # type: ignore[call-arg]
                except (TypeError, ValueError):
                    await bridge.open(call_ctx, session)
                sender_task = asyncio.create_task(
                    _paced_outbound_sender(),
                    name=f"twilio-out-{stream_sid}",
                )

            elif event == "media":
                payload = (msg.get("media") or {}).get("payload")
                if not payload:
                    continue
                # The decode is pure-NumPy table lookups (single-digit
                # microseconds per frame) and feed_audio() is a non-
                # blocking WS send. Keeping them on the loop avoids the
                # asyncio.to_thread round-trip overhead that costs more
                # than the operations themselves at telephony cadence.
                pcm16_16k_bytes = twilio_payload_to_pcm16_16k(payload)
                stats.audio_bytes_in += len(pcm16_16k_bytes)
                saa_buffer.extend(pcm16_16k_bytes)
                # Flush whole 100 ms frames; partial residue stays in the buffer.
                while len(saa_buffer) >= SAA_FRAME_BYTES:
                    frame = bytes(saa_buffer[:SAA_FRAME_BYTES])
                    del saa_buffer[:SAA_FRAME_BYTES]
                    # feed_audio expects raw int16 LE bytes; the SDK frames
                    # them as MSG_AUDIO and ships them on the SAA WebSocket.
                    saa.feed_audio(frame)

            elif event == "dtmf":
                digit = (msg.get("dtmf") or {}).get("digit", "")
                if digit:
                    await bridge.on_dtmf(digit)

            elif event == "mark":
                # Twilio confirms playback of a previously sent <mark>.
                # Bridges use this for end-of-utterance synchronisation
                # (e.g., to know when the agent's TTS has actually reached
                # the caller's ear before allowing barge-in).
                name = (msg.get("mark") or {}).get("name", "")
                if name:
                    with suppress(Exception):
                        await bridge.on_mark_played(name)

            elif event == "stop":
                log.info("[twilio %s] stop: stream=%s", conn_id, stream_sid)
                await _dispatch_hangup_once()
                break

            else:
                # Unknown / future event types, ignore per Twilio guidance.
                log.debug("[twilio %s] unknown event: %s", conn_id, event)

    except WebSocketDisconnect:
        log.info("[twilio %s] ws disconnect: stream=%s", conn_id, stream_sid)
        await _dispatch_hangup_once()
    except Exception:  # noqa: BLE001
        log.exception("[twilio %s] handler crashed (stream=%s)", conn_id, stream_sid)
        await _dispatch_hangup_once()
    finally:
        if session is not None:
            session._mark_closed()
        # Stop SAA first so its callback threads can't fire into a
        # partially-torn-down bridge while we shut everything else down.
        # Call synchronously: the SDK's stop() joins threads with a
        # bounded timeout, and going via asyncio.to_thread() races the
        # loop shutdown in test environments.
        if saa_started:
            with suppress(Exception):
                saa.stop()
        # Cancel + drain the sender before closing the bridge so the
        # last `mark_responding(False)` / outbound flush completes
        # cleanly. put_nowait(None) lets a sender currently blocked on
        # the queue exit without needing the cancel to land first.
        if sender_task is not None:
            with suppress(Exception):
                outbound_queue.put_nowait(None)
            sender_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await sender_task
        with suppress(Exception):
            await bridge.close()
        duration = time.monotonic() - stats.started_at
        with _AGGREGATE_LOCK:
            _AGGREGATE_STATS["calls_active"] -= 1
            _AGGREGATE_STATS["audio_bytes_in_total"] += stats.audio_bytes_in
            _AGGREGATE_STATS["audio_bytes_out_total"] += stats.audio_bytes_out
            _AGGREGATE_STATS["turn_ready_total"] += stats.turn_ready_count
            _AGGREGATE_STATS["barge_ins_total"] += stats.barge_ins
            _AGGREGATE_STATS["saa_errors_total"] += stats.saa_errors
            _AGGREGATE_STATS["saa_reconnects_total"] += stats.saa_reconnects
            _AGGREGATE_STATS["call_duration_seconds_total"] += duration
        log.info(
            "[twilio %s] call ended: stream=%s duration=%.1fs "
            "in=%d out=%d turns=%d barge_ins=%d saa_errors=%d saa_reconnects=%d",
            conn_id, stream_sid, duration,
            stats.audio_bytes_in, stats.audio_bytes_out, stats.turn_ready_count,
            stats.barge_ins, stats.saa_errors, stats.saa_reconnects,
        )


__all__ = ["app", "set_bridge_factory"]
