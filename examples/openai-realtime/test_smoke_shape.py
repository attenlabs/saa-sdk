"""Smoke test for examples/openai-realtime/server.py protocol shape.

Verifies the FastAPI relay's contract without hitting any network or
requiring fastapi/websockets/saa to be installed:

  - POST /session declared, returns ``client_secret`` from OpenAI sessions API
  - WebSocket /twilio declared with subprotocol ``audio.twilio.com``
  - GET /health declared
  - StaticFiles mount at "/" so the browser bundle is served alongside the API
  - Twilio bridge bridges µ-law 8 kHz ↔ PCM16 16 kHz ↔ PCM16 24 kHz
  - SAA client opened with enable_audio=False, enable_video=False on the
    server side (the v1.0 relay shim)
  - on_speech_ready hop into asyncio loop
  - mark_responding(True/False) drive SAA suppression during agent playback
  - Barge-in: response.cancel sent before forwarding a new utterance

No live LiveKit / OpenAI / Twilio server required.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SERVER = ROOT / "server.py"


def main() -> int:
    src = SERVER.read_text()
    try:
        tree = ast.parse(src, filename=str(SERVER))
    except SyntaxError as exc:
        print(f"✗ syntax error: {exc}", file=sys.stderr)
        return 1

    failures: list[str] = []

    def need(label: str, ok: bool) -> None:
        if not ok:
            failures.append(label)

    # ── routes ───────────────────────────────────────────────────────────────────
    need('POST /session route', '@app.post("/session")' in src)
    need('GET /health route', '@app.get("/health")' in src)
    need('WebSocket /twilio route', '@app.websocket("/twilio")' in src)
    need('StaticFiles mount serves browser bundle',
         'StaticFiles(' in src and 'html=True' in src)

    # ── OpenAI bits ──────────────────────────────────────────────────────────────────
    need('mints ephemeral session against /v1/realtime/sessions',
         '/v1/realtime/sessions' in src)
    need('uses Authorization: Bearer for sessions API',
         'Bearer ' in src and 'Authorization' in src)
    need('OpenAI-Beta: realtime=v1 header',
         'OpenAI-Beta' in src and 'realtime=v1' in src)
    need('opens Realtime WS', 'wss://api.openai.com/v1/realtime' in src)
    need('Realtime WS uses realtime + insecure-api-key + beta-v1 subprotocols',
         '"realtime"' in src
         and 'openai-insecure-api-key' in src
         and 'openai-beta.realtime-v1' in src)
    need('session.update pins pcm16 input + output',
         '"input_audio_format": "pcm16"' in src
         and '"output_audio_format": "pcm16"' in src)
    need('session.update disables OpenAI VAD (turn_detection=None)',
         '"turn_detection": None' in src)
    need('forwards utterance via input_audio_buffer.append',
         '"input_audio_buffer.append"' in src)
    need('commits + creates response on each utterance',
         '"input_audio_buffer.commit"' in src and '"response.create"' in src)

    # ── Twilio bridge ───────────────────────────────────────────────────────────────
    need('accepts Twilio audio.twilio.com subprotocol',
         'subprotocol="audio.twilio.com"' in src)
    need('handles Twilio start event for streamSid', '"start"' in src and 'streamSid' in src)
    need('handles Twilio media frames', '"media"' in src)
    need('handles Twilio stop event', '"stop"' in src)
    need('responds with media events to Twilio',
         '"event": "media"' in src or "'event': 'media'" in src)

    # ── audio plumbing ────────────────────────────────────────────────────────────────
    need('upsamples µ-law 8 kHz → PCM16 16 kHz', '_ulaw8k_to_pcm16_16k_b64' in src)
    need('downsamples PCM16 24 kHz → µ-law 8 kHz', '_pcm16_24k_to_ulaw8k_b64' in src)
    need('resamples SAA 16 kHz → OpenAI 24 kHz on the way in',
         'audioop.ratecv' in src and 'SAA_RATE' in src and 'OPENAI_RATE' in src)
    need('uses ulaw2lin for Twilio decode', 'audioop.ulaw2lin' in src)
    need('uses lin2ulaw for Twilio encode', 'audioop.lin2ulaw' in src)

    # ── SAA bits ─────────────────────────────────────────────────────────────────────
    need('AttentionClient created with enable_audio=False',
         'enable_audio=False' in src)
    need('AttentionClient created with enable_video=False',
         'enable_video=False' in src)
    need('on_speech_ready handler bridges to asyncio',
         'on_speech_ready' in src and 'run_coroutine_threadsafe' in src)
    need('mark_responding(True/False) used during agent playback',
         'mark_responding(True)' in src and 'mark_responding(False)' in src)
    need('barge-in via response.cancel before forwarding new turn',
         '"response.cancel"' in src or "'response.cancel'" in src)

    if failures:
        for f in failures:
            print(f"✗ {f}", file=sys.stderr)
        return 1

    # Sanity: confirm the AST parsed and at least the documented helpers exist.
    fns = {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for required in (
        'mint_session',
        'twilio_bridge',
        '_open_openai_realtime',
        '_twilio_inbound_loop',
        '_openai_inbound_loop',
        '_forward_to_openai',
        '_ulaw8k_to_pcm16_16k_b64',
        '_pcm16_24k_to_ulaw8k_b64',
    ):
        if required not in fns:
            print(f"✗ missing function: {required}", file=sys.stderr)
            return 1

    print(
        '✓ openai-realtime server shape: routes + ephemeral mint + '
        'twilio bridge + SAA gate + barge-in'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
