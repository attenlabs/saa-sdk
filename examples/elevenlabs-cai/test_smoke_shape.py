"""Smoke test for examples/elevenlabs-cai/server.py protocol shape.

Verifies the FastAPI process serves three roles in one binary:

  1. ``/twilio`` WebSocket relay: Twilio Media Streams ↔ SAA gate ↔
     raw ElevenLabs Conversational AI WebSocket (telephony path).
  2. ``/api/conversation-token`` / ``/api/signed-url`` /
     ``/api/conversation-config`` token-mint endpoints used by the
     browser bundle (so the ElevenLabs API key never leaves the server).
  3. Static file server for the browser bundle
     (``index.html`` + ``main.js``).

No network, no fastapi install required, opens the file with ``ast.parse``
and runs regex checks over the source so it's safe to run in CI without
the runtime dependencies installed.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SRV = ROOT / 'server.py'


def main() -> int:
    src = SRV.read_text()
    try:
        ast.parse(src, filename=str(SRV))
    except SyntaxError as e:
        print(f'✗ syntax error: {e}', file=sys.stderr); return 1

    failures = []

    # Twilio side.
    if 'audio.twilio.com' not in src:
        failures.append('Twilio Media Streams subprotocol "audio.twilio.com" not negotiated')
    if 'audioop.ulaw2lin' not in src:
        failures.append('inbound µ-law decode (audioop.ulaw2lin) not used')
    if 'audioop.lin2ulaw' not in src:
        failures.append('outbound µ-law encode (audioop.lin2ulaw) not used')
    if '8000, 16000' not in src:
        failures.append('upsample 8000 → 16000 not specified')
    if '16000, 8000' not in src:
        failures.append('downsample 16000 → 8000 not specified')

    # ElevenLabs side (WebSocket relay path).
    if 'api.elevenlabs.io/v1/convai/conversation' not in src:
        failures.append('ElevenLabs CAI WebSocket URL not referenced')
    if 'conversation_initiation_client_data' not in src:
        failures.append('conversation_initiation_client_data not sent on open')
    if 'user_input_audio_format' not in src or 'pcm_16000' not in src:
        failures.append('user_input_audio_format=pcm_16000 not declared')
    if '"user_audio_chunk"' not in src and "'user_audio_chunk'" not in src:
        failures.append('user_audio_chunk forward to ElevenLabs not present')
    if 'audio_event' not in src:
        failures.append('inbound audio_event handling missing')
    if '"ping"' not in src and "'ping'" not in src:
        failures.append('ping/pong handler missing (ElevenLabs requires pong)')
    if 'interruption' not in src:
        failures.append('interruption handler missing')
    if 'get-signed-url' not in src:
        failures.append('signed URL helper for private agents not referenced')

    # SAA side.
    if 'on_speech_ready' not in src:
        failures.append('on_speech_ready handler not registered')
    if 'mark_responding' not in src:
        failures.append('mark_responding helper missing')
    if '.mute()' not in src:
        failures.append('SAA mute() during agent playback missing')
    if 'enable_audio=False' not in src:
        failures.append('relay must disable SDK local-mic capture')
    if 'enable_video=False' not in src:
        failures.append('relay must disable SDK local-cam capture')

    # FastAPI surface.
    if '@app.websocket' not in src:
        failures.append('WebSocket route decorator not declared')
    if '@app.get("/health")' not in src and "@app.get('/health')" not in src:
        failures.append('/health endpoint missing')

    # Browser support: token-mint + static serve.
    if 'conversation-config' not in src:
        failures.append('/api/conversation-config endpoint missing (browser bootstrap)')
    if 'conversation-token' not in src:
        failures.append('/api/conversation-token endpoint missing (WebRTC mint)')
    if 'signed-url' not in src or 'mint_signed_url' not in src:
        failures.append('/api/signed-url endpoint missing (WebSocket mint)')
    if 'xi-api-key' not in src:
        failures.append('xi-api-key header not used for token mint')
    if 'StaticFiles' not in src:
        failures.append('static mount missing, browser bundle has nowhere to live')
    if 'SAA_GATE_MODE' not in src:
        failures.append('SAA_GATE_MODE env knob not surfaced')

    # Browser bundle present + uses both official SDKs.
    main_js = (ROOT / 'main.js').read_text()
    if '@elevenlabs/client' not in main_js:
        failures.append('main.js does not import @elevenlabs/client')
    if '@attenlabs/saa-js' not in main_js:
        failures.append('main.js does not import @attenlabs/saa-js')
    if 'Conversation.startSession' not in main_js:
        failures.append('main.js does not call Conversation.startSession')
    if 'setMicMuted' not in main_js:
        failures.append('main.js does not gate the EL mic via setMicMuted')
    if 'sendContextualUpdate' not in main_js:
        failures.append('main.js does not emit sendContextualUpdate from SAA signals')
    if 'clientTools' not in main_js:
        failures.append('main.js does not register clientTools')

    if failures:
        for f in failures:
            print(f'✗ {f}', file=sys.stderr)
        return 1
    print(
        '✓ elevenlabs-cai server + browser shape: telephony relay + '
        'token-mint + static + SDK-based browser flow with rich SAA signals.'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
