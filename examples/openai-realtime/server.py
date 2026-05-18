"""SAA × OpenAI Realtime production relay.

A single FastAPI app that does the three things every production deployment
of an OpenAI Realtime voice agent needs:

1. ``POST /session``, Mints a short-lived OpenAI Realtime ``client_secret``
   server-side and returns it to the browser. The OpenAI API key never
   leaves this process. Mirrors the contract documented at
   https://platform.openai.com/docs/guides/realtime so any official browser
   client (including ``main.js`` shipped alongside this file) works
   out-of-the-box.

2. ``WebSocket /twilio``, A Twilio Media Streams bridge. Twilio sends
   µ-law @ 8 kHz over WebSocket; we run it through SAA at 16 kHz, forward
   only device-directed utterances to OpenAI Realtime at 24 kHz, then
   downsample OpenAI's 24 kHz response audio back to µ-law for Twilio.
   The phone caller never reaches the LLM unless SAA classified them as
   addressing the device.

3. ``GET /``, Static file server for the browser bundle (``index.html``
   and ``main.js``) so the same process can serve the demo and mint
   ephemeral tokens behind one origin.

Why this layout
---------------
A pure browser demo is the fastest way to *try* the integration; a relay
is the only way to *ship* it. The integration story changes very little:
the browser still uses ``input_audio_buffer.append`` / ``response.create``
and SAA still emits ``speechReady``. What changes is *who holds the OpenAI
key* and *whether phone calls are first-class*. Both are wired here.

Verified against ``openai`` Realtime API (subprotocol ``realtime``, model
``gpt-realtime``), ``attenlabs-saa`` 1.0.0, and Twilio Media Streams
(https://www.twilio.com/docs/voice/media-streams). The relay constructs
``AttentionClient(enable_audio=False, enable_video=False)`` and forwards
phone audio with the public ``feed_audio()`` API.
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
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as exc:  # pragma: no cover
    raise SystemExit("fastapi required: pip install -r requirements.txt") from exc

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
except ImportError as exc:  # pragma: no cover
    raise SystemExit("websockets required: pip install -r requirements.txt") from exc

from saa import AttentionClient, SpeechReadyEvent


# ── configuration ───────────────────────────────────────────────────────────

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_REALTIME_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime")
OPENAI_VOICE = os.environ.get("OPENAI_VOICE", "alloy")
OPENAI_REALTIME_WS = os.environ.get(
    "OPENAI_REALTIME_WS",
    "wss://api.openai.com/v1/realtime?model=gpt-realtime",
)
OPENAI_SESSIONS_URL = os.environ.get(
    "OPENAI_SESSIONS_URL",
    "https://api.openai.com/v1/realtime/sessions",
)

ATTENLABS_TOKEN = os.environ.get("ATTENLABS_TOKEN", "")
ATTENLABS_URL = os.environ.get("ATTENLABS_URL")  # falls back to SDK default
SAA_THRESHOLD = float(os.environ.get("SAA_THRESHOLD", "0.7"))

DEFAULT_INSTRUCTIONS = os.environ.get(
    "OPENAI_INSTRUCTIONS",
    "You are a helpful, concise voice assistant. Reply in one or two short "
    "sentences. When the user asks about weather or timers, use the provided "
    "tools instead of making up answers.",
)

SAA_RATE = 16_000
OPENAI_RATE = 24_000
TWILIO_RATE = 8_000

ROOT = pathlib.Path(__file__).resolve().parent

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("saa-openai-relay")

app = FastAPI(title="saa × openai-realtime relay")

# Mount static files at the end so /session and /twilio take priority.
# (Registered in `__main__` block below to keep ordering explicit.)


# ── ephemeral token endpoint ────────────────────────────────────────────────


@app.post("/session")
async def mint_session(req: Request) -> JSONResponse:
    """Mint an ephemeral OpenAI Realtime session and return ``client_secret``.

    The browser sends a partial session config (model, voice, tools); we
    forward it to OpenAI with our API key, and pass the response straight
    back. The browser uses ``client_secret.value`` as its WebSocket
    subprotocol token, so the OpenAI key never leaves this process.
    """
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")

    payload: dict[str, Any] = {}
    try:
        if int(req.headers.get("content-length", "0") or "0") > 0:
            payload = await req.json()
    except Exception:
        payload = {}

    body = {
        "model": payload.get("model") or OPENAI_REALTIME_MODEL,
        "voice": payload.get("voice") or OPENAI_VOICE,
        "instructions": payload.get("instructions") or DEFAULT_INSTRUCTIONS,
        "modalities": payload.get("modalities") or ["text", "audio"],
        "input_audio_format": payload.get("input_audio_format") or "pcm16",
        "output_audio_format": payload.get("output_audio_format") or "pcm16",
        # SAA does endpointing; OpenAI's built-in VAD must be off, otherwise
        # the two systems fight each other and the agent answers half-turns.
        "turn_detection": payload.get("turn_detection"),
    }
    if "tools" in payload:
        body["tools"] = payload["tools"]
    if "tool_choice" in payload:
        body["tool_choice"] = payload["tool_choice"]

    try:
        result = await asyncio.to_thread(_post_openai_session, body)
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        detail = exc.read().decode("utf-8", errors="replace")
        logger.warning("openai sessions HTTP %s: %s", exc.code, detail)
        raise HTTPException(status_code=exc.code, detail=detail) from exc
    except Exception as exc:
        logger.exception("openai sessions failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return JSONResponse(result)


def _post_openai_session(body: dict) -> dict:
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        OPENAI_SESSIONS_URL,
        data=raw,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
            "OpenAI-Beta": "realtime=v1",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── Twilio Media Streams bridge ──────────────────────────────────────────────


@dataclass
class _PhoneSession:
    """Per-call state for a Twilio ↔ SAA ↔ OpenAI bridge."""
    twilio: WebSocket
    openai: WebSocketClientProtocol
    saa: AttentionClient
    stream_sid: str = ""
    agent_speaking: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    ratecv_in_state: Any = None   # audioop.ratecv state for 8k→16k
    ratecv_saa_to_oa: Any = None  # 16k→24k
    ratecv_out_state: Any = None  # 24k→8k


@app.websocket("/twilio")
async def twilio_bridge(ws: WebSocket) -> None:
    """Twilio Media Streams ↔ SAA gate ↔ OpenAI Realtime.

    Per-call lifecycle:

      1. Twilio negotiates the ``audio.twilio.com`` subprotocol.
      2. We open a parallel WebSocket to OpenAI Realtime (subprotocols
         ``realtime`` + ``openai-insecure-api-key.<API_KEY>`` +
         ``openai-beta.realtime-v1``) and send a ``session.update`` to
         pin ``input_audio_format=pcm16`` (24 kHz) and ``turn_detection=null``.
      3. We open a SAA ``AttentionClient`` with mic+cam disabled and feed
         it 16 kHz PCM16 from each Twilio media frame.
      4. On ``speech_ready``: resample 16→24 kHz, base64-encode, send to
         OpenAI as ``input_audio_buffer.append`` + commit + ``response.create``.
      5. OpenAI streams 24 kHz PCM16 back; we resample to 8 kHz µ-law and
         send to Twilio as ``media`` events with the call's ``streamSid``.
      6. ``response.done`` clears the speaking flag and unmutes SAA;
         ``input_audio_buffer.speech_started`` (which only fires if SAA *also*
         classified the audio as device-directed and we forwarded it) is
         used to inform the UI.
    """
    await ws.accept(subprotocol="audio.twilio.com")

    if not OPENAI_API_KEY:
        await ws.close(code=1011, reason="OPENAI_API_KEY not configured")
        return
    if not ATTENLABS_TOKEN:
        await ws.close(code=1011, reason="ATTENLABS_TOKEN not configured")
        return

    try:
        openai = await _open_openai_realtime()
    except Exception as exc:
        logger.exception("openai realtime open failed")
        await ws.close(code=1011, reason=f"openai unreachable: {exc}")
        return

    saa = AttentionClient(
        token=ATTENLABS_TOKEN,
        url=ATTENLABS_URL,
        enable_audio=False,
        enable_video=False,
        initial_threshold=SAA_THRESHOLD,
    )
    sess = _PhoneSession(twilio=ws, openai=openai, saa=saa)
    loop = asyncio.get_running_loop()

    @saa.on_speech_ready
    def _on_speech(event: SpeechReadyEvent) -> None:
        # Fires on the SAA WS thread; bounce to the asyncio loop to send.
        asyncio.run_coroutine_threadsafe(
            _forward_to_openai(sess, event.audio_pcm16.tobytes()),
            loop,
        )

    @saa.on_error
    def _on_err(e) -> None:
        logger.warning("saa error: %s, %s", e.title, e.message)

    saa.start()

    try:
        await asyncio.gather(
            _twilio_inbound_loop(sess),
            _openai_inbound_loop(sess),
        )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("twilio bridge crashed")
    finally:
        sess.closed.set()
        saa.stop()
        try:
            await openai.close()
        except Exception:
            pass


async def _open_openai_realtime() -> WebSocketClientProtocol:
    """Open the OpenAI Realtime WebSocket and send the initial session.update.

    OpenAI accepts the API key via WebSocket subprotocol negotiation; the
    ``openai-insecure-api-key.<key>`` token is what the browser uses too,
    documented in the Realtime guide. We pin ``turn_detection=null`` so
    OpenAI's built-in VAD is disabled, SAA has already endpointed by the
    time anything reaches OpenAI.
    """
    ws = await websockets.connect(
        OPENAI_REALTIME_WS,
        subprotocols=[
            "realtime",
            f"openai-insecure-api-key.{OPENAI_API_KEY}",
            "openai-beta.realtime-v1",
        ],
        max_size=2 ** 24,
    )
    await ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "instructions": DEFAULT_INSTRUCTIONS,
            "voice": OPENAI_VOICE,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": None,
            "input_audio_transcription": {"model": "whisper-1"},
        },
    }))
    return ws


async def _twilio_inbound_loop(sess: _PhoneSession) -> None:
    """Pump Twilio media frames into SAA as PCM16 @ 16 kHz."""
    while not sess.closed.is_set():
        raw = await sess.twilio.receive_text()
        msg = json.loads(raw)
        event = msg.get("event")

        if event == "start":
            sess.stream_sid = msg.get("start", {}).get("streamSid", "")
            logger.info("twilio call started: %s", sess.stream_sid)

        elif event == "media":
            payload_b64 = msg.get("media", {}).get("payload", "")
            if not payload_b64:
                continue
            pcm16_16k, sess.ratecv_in_state = await asyncio.to_thread(
                _ulaw8k_to_pcm16_16k_b64, payload_b64, sess.ratecv_in_state,
            )
            await asyncio.to_thread(sess.saa.feed_audio, pcm16_16k)

        elif event == "stop":
            logger.info("twilio call stopped: %s", sess.stream_sid)
            sess.closed.set()
            return


async def _openai_inbound_loop(sess: _PhoneSession) -> None:
    """Pump OpenAI events back to Twilio."""
    async for raw in sess.openai:
        if isinstance(raw, bytes):
            continue
        msg = json.loads(raw)
        t = msg.get("type")

        if t == "response.audio.delta":
            audio_b64 = msg.get("delta", "")
            if not audio_b64:
                continue
            if not sess.agent_speaking:
                sess.agent_speaking = True
                sess.saa.mark_responding(True)
            ulaw_b64, sess.ratecv_out_state = await asyncio.to_thread(
                _pcm16_24k_to_ulaw8k_b64, audio_b64, sess.ratecv_out_state,
            )
            await sess.twilio.send_text(json.dumps({
                "event": "media",
                "streamSid": sess.stream_sid,
                "media": {"payload": ulaw_b64},
            }))

        elif t == "response.done":
            _clear_agent_speaking(sess)

        elif t == "input_audio_buffer.speech_started":
            # Useful for diagnostics only; SAA already gated the audio.
            logger.debug("openai noted speech start")

        elif t == "conversation.item.input_audio_transcription.completed":
            text = msg.get("transcript", "")
            if text:
                logger.info("caller: %s", text)

        elif t == "response.audio_transcript.done":
            text = msg.get("transcript", "")
            if text:
                logger.info("agent: %s", text)

        elif t == "error":
            err = msg.get("error", {})
            logger.warning("openai realtime error: %s", err)


def _clear_agent_speaking(sess: _PhoneSession) -> None:
    if sess.agent_speaking:
        sess.agent_speaking = False
        sess.saa.mark_responding(False)


async def _forward_to_openai(sess: _PhoneSession, pcm16_16k: bytes) -> None:
    """Forward a SAA-gated utterance to OpenAI Realtime."""
    if not pcm16_16k:
        return
    pcm16_24k, sess.ratecv_saa_to_oa = await asyncio.to_thread(
        audioop.ratecv, pcm16_16k, 2, 1, SAA_RATE, OPENAI_RATE, sess.ratecv_saa_to_oa,
    )
    audio_b64 = base64.b64encode(pcm16_24k).decode()
    try:
        # Barge-in: cancel any in-flight response before committing the new
        # turn. The model returns an error if there's nothing to cancel, we
        # ignore it via a separate try because the cost of one extra round-
        # trip beats the cost of two responses talking over each other.
        if sess.agent_speaking:
            try:
                await sess.openai.send(json.dumps({"type": "response.cancel"}))
            except Exception:
                pass
            _clear_agent_speaking(sess)
        await sess.openai.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": audio_b64,
        }))
        await sess.openai.send(json.dumps({"type": "input_audio_buffer.commit"}))
        await sess.openai.send(json.dumps({"type": "response.create"}))
    except websockets.exceptions.ConnectionClosed:
        sess.closed.set()


# ── audio plumbing ───────────────────────────────────────────────────────────


def _ulaw8k_to_pcm16_16k_b64(b64_ulaw: str, state: Any) -> tuple[bytes, Any]:
    raw_mulaw = base64.b64decode(b64_ulaw)
    pcm16_8k = audioop.ulaw2lin(raw_mulaw, 2)
    pcm16_16k, new_state = audioop.ratecv(
        pcm16_8k, 2, 1, TWILIO_RATE, SAA_RATE, state,
    )
    return pcm16_16k, new_state


def _pcm16_24k_to_ulaw8k_b64(b64_pcm: str, state: Any) -> tuple[str, Any]:
    pcm16_24k = base64.b64decode(b64_pcm)
    pcm16_8k, new_state = audioop.ratecv(
        pcm16_24k, 2, 1, OPENAI_RATE, TWILIO_RATE, state,
    )
    ulaw = audioop.lin2ulaw(pcm16_8k, 2)
    return base64.b64encode(ulaw).decode(), new_state


# ── health + static ────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    """Configuration sanity check. Useful for liveness / readiness probes."""
    return {
        "ok": True,
        "openai_key_configured": bool(OPENAI_API_KEY),
        "openai_model": OPENAI_REALTIME_MODEL,
        "saa_token_configured": bool(ATTENLABS_TOKEN),
        "saa_threshold": SAA_THRESHOLD,
        "saa_url": ATTENLABS_URL or "<sdk default>",
    }


# Static file mount comes last so /session and /twilio aren't shadowed.
app.mount(
    "/",
    StaticFiles(directory=str(ROOT), html=True),
    name="static",
)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(
        "server:app",
        host=host,
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        reload=os.environ.get("RELOAD", "").lower() in ("1", "true", "yes"),
    )
