"""SAA gating between Twilio Media Streams and ElevenLabs Conversational AI.

A FastAPI WebSocket relay that bridges a Twilio phone call to an ElevenLabs
CAI agent, with SAA deciding which speech actually reaches the agent. Only
utterances classified as device-directed (cls 2) are forwarded as
``user_audio_chunk`` events; everything else (background media playback, the
caller talking to someone else, line noise) is silently dropped before the
agent spends an LLM token or a TTS character on it.

Wire shape
----------
- Twilio sends µ-law @ 8 kHz, base64-encoded, over WebSocket subprotocol
  ``audio.twilio.com`` (documented at
  https://www.twilio.com/docs/voice/media-streams/websocket-messages).
- ElevenLabs CAI accepts PCM16 @ 16 kHz, base64-encoded, as
  ``{"user_audio_chunk": "<base64>"}`` over WebSocket
  (documented at https://elevenlabs.io/docs/conversational-ai/api-reference
  /conversational-ai/websocket).
- SAA sits in between: receives PCM16 @ 16 kHz, emits ``speech_ready`` with
  PCM16 @ 16 kHz utterances ready to forward.

The relay also forwards the agent's audio back down to Twilio (PCM16 @ 16 kHz
→ µ-law @ 8 kHz), suppresses SAA predictions while the agent is speaking,
and handles interruptions cleanly.

Status
------
Reference adapter for the May 19, 2026 v1.0 launch. The v1.0 SAA SDK
captures the local mic by default; this relay disables that path with
``enable_audio=False, enable_video=False`` and feeds Twilio audio in via
``AttentionClient.feed_audio()``. The same shape is used by the
``examples/twilio/`` and ``examples/pipecat/`` adapters.
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging
import os
import pathlib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as exc:  # pragma: no cover
    raise SystemExit("fastapi required: pip install fastapi uvicorn") from exc

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
except ImportError as exc:  # pragma: no cover
    raise SystemExit("websockets required: pip install websockets") from exc

from saa import AttentionClient, SpeechReadyEvent

ROOT = pathlib.Path(__file__).resolve().parent


# ── configuration ─────────────────────────────────────────────────

ELEVENLABS_AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_WS_BASE = os.environ.get(
    "ELEVENLABS_WS_BASE",
    "wss://api.elevenlabs.io/v1/convai/conversation",
)
ELEVENLABS_SIGNED_URL_BASE = os.environ.get(
    "ELEVENLABS_SIGNED_URL_BASE",
    "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url",
)
ATTENLABS_TOKEN = os.environ.get("ATTENLABS_TOKEN", "")
SAA_THRESHOLD = float(os.environ.get("SAA_THRESHOLD", "0.7"))
# How SAA gates the ElevenLabs mic in the browser app:
#   mic    , SAA's prediction drives setMicMuted (default)
#   context, never mute; only annotate via sendContextualUpdate
#   off    , SAA observer-only (no mic gate, no contextual updates)
SAA_GATE_MODE = os.environ.get("SAA_GATE_MODE", "mic").strip().lower()
ELEVENLABS_CONNECTION_TYPE = os.environ.get(
    "ELEVENLABS_CONNECTION_TYPE", "webrtc",
).strip().lower()
ELEVENLABS_TOKEN_URL = os.environ.get(
    "ELEVENLABS_TOKEN_URL",
    "https://api.elevenlabs.io/v1/convai/conversation/token",
)

logger = logging.getLogger("saa-elevenlabs-relay")

app = FastAPI(title="saa × elevenlabs-cai relay")


# ── ElevenLabs CAI WebSocket helpers ──────────────────────────────


async def _open_elevenlabs(agent_id: str) -> WebSocketClientProtocol:
    """Open a WebSocket to an ElevenLabs Conversational AI agent.

    Public agents accept ``?agent_id=<id>`` directly. Private agents require
    a signed URL minted from the REST API with ``xi-api-key``. We honour
    both: if ``ELEVENLABS_API_KEY`` is set we mint a signed URL; otherwise
    we connect with the bare ``agent_id`` query param.
    """
    if not agent_id:
        raise RuntimeError("ELEVENLABS_AGENT_ID not set")

    if ELEVENLABS_API_KEY:
        url = await _mint_signed_url(agent_id)
    else:
        url = f"{ELEVENLABS_WS_BASE}?agent_id={agent_id}"

    return await websockets.connect(url, max_size=2**24)


async def _mint_signed_url(agent_id: str) -> str:
    """REST: GET /v1/convai/conversation/get-signed-url?agent_id=...

    Returns ``{\"signed_url\": \"wss://...\"}``. Required for private agents
    so the API key never crosses an untrusted hop.
    """
    qs = urllib.parse.urlencode({"agent_id": agent_id})
    req = urllib.request.Request(
        f"{ELEVENLABS_SIGNED_URL_BASE}?{qs}",
        headers={"xi-api-key": ELEVENLABS_API_KEY},
    )
    ctx = ssl.create_default_context()
    loop = asyncio.get_running_loop()
    body = await loop.run_in_executor(
        None,
        lambda: urllib.request.urlopen(req, context=ctx, timeout=10).read(),
    )
    payload = json.loads(body.decode())
    signed = payload.get("signed_url")
    if not signed:
        raise RuntimeError(f"signed-url response missing 'signed_url': {payload!r}")
    return signed


async def _send_initiation(eleven: WebSocketClientProtocol) -> None:
    """Send the initiation client_data frame so the agent knows our audio shape.

    Telling ElevenLabs ``user_input_audio_format=pcm_16000`` is what makes
    the SAA-gated PCM16 @ 16 kHz audio a one-line forward (no resample).
    """
    await eleven.send(json.dumps({
        "type": "conversation_initiation_client_data",
        "conversation_config_override": {
            "agent": {
                "first_message_only_initially": True,
            },
        },
        # Match the SAA speech_ready output exactly so we never resample.
        "custom_llm_extra_body": {},
        "user_input_audio_format": "pcm_16000",
        "agent_output_audio_format": "pcm_16000",
    }))


# ── per-call session state ────────────────────────────────────────


# How long the agent's audio stream must be quiet before we declare
# the agent done speaking. ElevenLabs CAI does NOT emit an explicit
# "agent finished" event on its WebSocket; the agent stops by simply
# ceasing to send `audio` chunks, so we debounce.
AGENT_QUIET_THRESHOLD_S = 0.6


@dataclass
class _Session:
    twilio: WebSocket
    eleven: WebSocketClientProtocol
    saa: AttentionClient
    twilio_stream_sid: str = ""
    agent_speaking: bool = False
    last_agent_audio_at: float = 0.0
    closed: asyncio.Event = field(default_factory=asyncio.Event)


# ── audio plumbing ────────────────────────────────────────────────


def _mulaw_8k_to_pcm16_16k(b64_ulaw: str) -> bytes:
    raw_mulaw = base64.b64decode(b64_ulaw)
    pcm16_8k = audioop.ulaw2lin(raw_mulaw, 2)
    pcm16_16k, _ = audioop.ratecv(pcm16_8k, 2, 1, 8000, 16000, None)
    return pcm16_16k


def _pcm16_16k_to_mulaw_8k_b64(pcm16_16k: bytes) -> str:
    pcm16_8k, _ = audioop.ratecv(pcm16_16k, 2, 1, 16000, 8000, None)
    ulaw = audioop.lin2ulaw(pcm16_8k, 2)
    return base64.b64encode(ulaw).decode()


# ── the relay endpoint ────────────────────────────────────────────


@app.websocket("/twilio")
async def twilio_to_elevenlabs(ws: WebSocket) -> None:
    """Twilio Media Streams ↔ SAA gate ↔ ElevenLabs CAI.

    The lifecycle of one call:

        1. Twilio negotiates the ``audio.twilio.com`` subprotocol.
        2. We open a parallel WebSocket to the ElevenLabs CAI agent and
           send ``conversation_initiation_client_data``.
        3. We open a SAA ``AttentionClient`` with mic+cam capture disabled
           (``enable_audio=False, enable_video=False``) and subscribe to
           ``speech_ready``. SAA runs cloud-side classification on whatever
           PCM16 we feed it.
        4. For every Twilio media frame: decode µ-law 8 kHz → PCM16 16 kHz
           and feed it to SAA via the SDK's mic-callback hook. (See the
           v1.1 signaling-only note in the module docstring.)
        5. On ``speech_ready``: forward the base64 PCM16 to ElevenLabs as
           ``{\"user_audio_chunk\": ...}``. Mute SAA, mark responding.
        6. ElevenLabs streams agent audio frames back. We base64→PCM16,
           resample to 8 kHz µ-law, and send to Twilio as ``media`` events.
        7. On ``response`` end / ``interruption``, unmute SAA and clear
           the responding flag.
    """
    await ws.accept(subprotocol="audio.twilio.com")
    if not ATTENLABS_TOKEN:
        await ws.close(code=1011, reason="ATTENLABS_TOKEN not configured")
        return

    try:
        eleven = await _open_elevenlabs(ELEVENLABS_AGENT_ID)
    except Exception as exc:
        logger.exception("failed to open ElevenLabs CAI WebSocket")
        await ws.close(code=1011, reason=f"elevenlabs unreachable: {exc}")
        return

    # Tell ElevenLabs about the audio formats up front so the agent
    # accepts our SAA-gated PCM16 frames without resampling on its side.
    await _send_initiation(eleven)

    saa = AttentionClient(
        token=ATTENLABS_TOKEN,
        enable_audio=False,
        enable_video=False,
        initial_threshold=SAA_THRESHOLD,
    )
    sess = _Session(twilio=ws, eleven=eleven, saa=saa)
    loop = asyncio.get_running_loop()

    @saa.on_speech_ready
    def _on_speech(event: SpeechReadyEvent) -> None:
        # Fires on the SAA WS thread; hop back to the asyncio loop.
        asyncio.run_coroutine_threadsafe(
            _forward_to_elevenlabs(sess, event.audio_base64),
            loop,
        )

    @saa.on_error
    def _on_err(e) -> None:
        logger.warning("saa error: %s, %s", e.title, e.message)

    saa.start()

    # Run all three loops concurrently. Whichever finishes first
    # (Twilio hangup, ElevenLabs disconnect, or watchdog crash) wins;
    # the others are cancelled so we never leak a half-open relay.
    twilio_task = asyncio.create_task(_twilio_inbound_loop(sess))
    eleven_task = asyncio.create_task(_elevenlabs_inbound_loop(sess))
    watchdog_task = asyncio.create_task(_agent_quiet_watchdog(sess, loop))

    try:
        done, pending = await asyncio.wait(
            {twilio_task, eleven_task, watchdog_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, WebSocketDisconnect):
                pass
        # Re-raise the first non-cancelled exception so it surfaces in
        # logs (rather than being swallowed by FIRST_COMPLETED).
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                logger.warning("relay loop exited with: %r", exc)
    finally:
        sess.closed.set()
        saa.stop()
        try:
            await eleven.close()
        except Exception:
            pass


async def _twilio_inbound_loop(sess: _Session) -> None:
    """Pump Twilio media frames into SAA as PCM16 @ 16 kHz."""
    try:
        while not sess.closed.is_set():
            raw = await sess.twilio.receive_text()
            msg = json.loads(raw)
            event = msg.get("event")
            if event == "start":
                sess.twilio_stream_sid = msg.get("start", {}).get("streamSid", "")
                logger.info("twilio call started: %s", sess.twilio_stream_sid)
            elif event == "media":
                pcm16_16k = await asyncio.to_thread(
                    _mulaw_8k_to_pcm16_16k, msg["media"]["payload"],
                )
                await asyncio.to_thread(sess.saa.feed_audio, pcm16_16k)
            elif event == "mark":
                # Twilio echoes our outbound `mark` events. Ignore.
                pass
            elif event == "stop":
                logger.info("twilio call stopped")
                return
    finally:
        sess.closed.set()


async def _elevenlabs_inbound_loop(sess: _Session) -> None:
    """Pump ElevenLabs agent audio + control events back to Twilio."""
    async for raw in sess.eleven:
        if isinstance(raw, bytes):
            continue
        msg = json.loads(raw)
        t = msg.get("type")

        if t == "audio":
            audio_b64 = msg.get("audio_event", {}).get("audio_base_64", "")
            if audio_b64:
                if not sess.agent_speaking:
                    sess.agent_speaking = True
                    sess.saa.mark_responding(True)
                    sess.saa.mute()
                sess.last_agent_audio_at = asyncio.get_running_loop().time()
                pcm16_16k = base64.b64decode(audio_b64)
                ulaw_b64 = _pcm16_16k_to_mulaw_8k_b64(pcm16_16k)
                await sess.twilio.send_text(json.dumps({
                    "event": "media",
                    "streamSid": sess.twilio_stream_sid,
                    "media": {"payload": ulaw_b64},
                }))

        elif t == "agent_response" or t == "agent_response_correction":
            # Text-only event; useful for transcripts and logging.
            text = msg.get("agent_response_event", {}).get("agent_response", "")
            logger.info("agent: %s", text)

        elif t == "user_transcript":
            text = msg.get("user_transcription_event", {}).get("user_transcript", "")
            logger.info("user: %s", text)

        elif t == "interruption":
            # User cut the agent off. Clear responding state and let SAA
            # resume listening immediately.
            await sess.twilio.send_text(json.dumps({
                "event": "clear",
                "streamSid": sess.twilio_stream_sid,
            }))
            _clear_agent_speaking(sess)

        elif t == "ping":
            ping_evt = msg.get("ping_event", {}) or {}
            event_id = ping_evt.get("event_id")
            if event_id is not None:
                # The optional ping_ms tells you how many ms ElevenLabs
                # is waiting before it considers the connection stale.
                # We respond immediately so this is essentially free.
                await sess.eleven.send(json.dumps({
                    "type": "pong",
                    "event_id": event_id,
                }))

        elif t == "client_tool_call":
            # Tool calls live entirely above this relay, apps that need
            # them should subscribe via the ElevenLabs SDK directly. Here
            # we just no-op and let the agent recover.
            pass

        elif t == "conversation_initiation_metadata":
            meta = msg.get("conversation_initiation_metadata_event", {})
            logger.info("elevenlabs ready: %s", meta.get("conversation_id", "?"))

    # If we fall out of the async-for, the ElevenLabs WS closed; signal
    # the rest of the relay to tear down.
    sess.closed.set()


def _clear_agent_speaking(sess: _Session) -> None:
    if sess.agent_speaking:
        sess.agent_speaking = False
        sess.saa.mark_responding(False)
        sess.saa.unmute()
        logger.debug("agent done, unmuted SAA, listening again")


async def _agent_quiet_watchdog(sess: _Session, loop: asyncio.AbstractEventLoop) -> None:
    """Clear ``agent_speaking`` once no audio chunk has arrived for AGENT_QUIET_THRESHOLD_S.

    The ElevenLabs CAI WebSocket has no explicit "agent done speaking"
    event; the agent simply stops sending ``audio`` events. We detect
    that by watching ``last_agent_audio_at`` and unmuting SAA once the
    stream has been quiet for AGENT_QUIET_THRESHOLD_S.
    """
    while not sess.closed.is_set():
        await asyncio.sleep(0.1)
        if not sess.agent_speaking:
            continue
        if loop.time() - sess.last_agent_audio_at > AGENT_QUIET_THRESHOLD_S:
            _clear_agent_speaking(sess)


async def _forward_to_elevenlabs(sess: _Session, audio_base64: str) -> None:
    """Forward a SAA-gated utterance to the ElevenLabs CAI agent."""
    if not audio_base64:
        return
    try:
        await sess.eleven.send(json.dumps({
            "user_audio_chunk": audio_base64,
        }))
    except websockets.exceptions.ConnectionClosed:
        sess.closed.set()


# ── health check ──────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "agent_id_configured": bool(ELEVENLABS_AGENT_ID),
        "private_agent": bool(ELEVENLABS_API_KEY),
        "saa_token_configured": bool(ATTENLABS_TOKEN),
        "threshold": SAA_THRESHOLD,
        "gate_mode": SAA_GATE_MODE,
        "connection_type": ELEVENLABS_CONNECTION_TYPE,
    }


# Browser config + token-mint endpoints. The browser hits these three
# routes on this same process so secrets never cross the wire to the
# client. The xi-api-key stays in ELEVENLABS_API_KEY; the page sees only
# the short-lived blobs the ElevenLabs API returns.


@app.get("/api/conversation-config")
async def conversation_config() -> JSONResponse:
    """Tell the browser how to call both SDKs without baking secrets."""
    if not ELEVENLABS_AGENT_ID:
        return JSONResponse(
            status_code=503,
            content={"error": "ELEVENLABS_AGENT_ID is not configured on the server"},
        )
    return JSONResponse({
        "agentId": ELEVENLABS_AGENT_ID,
        "connectionType": ELEVENLABS_CONNECTION_TYPE,
        "saaThreshold": SAA_THRESHOLD,
        "saaGateMode": SAA_GATE_MODE,
        "authMode": "private" if ELEVENLABS_API_KEY else "public",
    })


def _elevenlabs_api_get(url: str) -> dict[str, Any]:
    """GET ``url`` with ``xi-api-key`` and return the parsed JSON body."""
    if not ELEVENLABS_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="ELEVENLABS_API_KEY is not configured on the server",
        )
    req = urllib.request.Request(
        url,
        headers={"xi-api-key": ELEVENLABS_API_KEY, "Accept": "application/json"},
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.warning("ElevenLabs %s returned %s: %s", url, exc.code, body[:200])
        raise HTTPException(status_code=exc.code, detail=body) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("ElevenLabs API call failed (%s)", url)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/conversation-token")
async def mint_conversation_token() -> JSONResponse:
    """Mint a short-lived WebRTC conversation token (xi-api-key stays here).

    Reference: https://elevenlabs.io/docs/api-reference/conversations/get-webrtc-token
    """
    if not ELEVENLABS_AGENT_ID:
        return JSONResponse(
            status_code=503,
            content={"error": "ELEVENLABS_AGENT_ID is not configured"},
        )
    url = (
        f"{ELEVENLABS_TOKEN_URL}?"
        + urllib.parse.urlencode({"agent_id": ELEVENLABS_AGENT_ID})
    )
    body = await asyncio.to_thread(_elevenlabs_api_get, url)
    if not body.get("token"):
        return JSONResponse(
            status_code=502,
            content={"error": "ElevenLabs response missing 'token'", "body": body},
        )
    logger.info("minted WebRTC conversation token for agent=%s", ELEVENLABS_AGENT_ID)
    return JSONResponse({"token": body["token"]})


@app.get("/api/signed-url")
async def mint_signed_url() -> JSONResponse:
    """Mint a signed WebSocket URL (xi-api-key stays here).

    Reference: https://elevenlabs.io/docs/conversational-ai/customization/authentication
    """
    if not ELEVENLABS_AGENT_ID:
        return JSONResponse(
            status_code=503,
            content={"error": "ELEVENLABS_AGENT_ID is not configured"},
        )
    url = (
        f"{ELEVENLABS_SIGNED_URL_BASE}?"
        + urllib.parse.urlencode({"agent_id": ELEVENLABS_AGENT_ID})
    )
    body = await asyncio.to_thread(_elevenlabs_api_get, url)
    signed = body.get("signed_url")
    if not signed:
        return JSONResponse(
            status_code=502,
            content={"error": "ElevenLabs response missing 'signed_url'", "body": body},
        )
    logger.info("minted signed WebSocket URL for agent=%s", ELEVENLABS_AGENT_ID)
    return JSONResponse({"signedUrl": signed})


# Mount last so the WebSocket route and /api/* HTTP routes registered
# above take priority. The bundle is small enough (index.html + main.js)
# that we serve it from disk directly; for production you can equally
# put it behind a CDN.
app.mount(
    "/",
    StaticFiles(directory=str(ROOT), html=True),
    name="static",
)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "server:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        reload=os.environ.get("RELOAD", "").lower() in ("1", "true", "yes"),
    )
