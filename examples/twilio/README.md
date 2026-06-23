# SAA + Twilio Media Streams

SAA is a pre-STT device-directed gate for inbound and outbound phone calls over Twilio Media Streams. The adapter in `media_streams/server.py` receives the raw telephony stream from Twilio, transcodes μ-law 8 kHz frames to PCM16 16 kHz, and feeds them to SAA's `feed_audio` API. Only device-directed speech reaches your STT, LLM, or TTS; side talk, hold music, and the agent's own TTS echo are gated out.

## How SAA integrates

1. Twilio `<Connect><Stream>` opens a WebSocket to `server.py`'s `/twilio` endpoint.
2. `server.py` receives inbound `media` events containing base64-encoded G.711 μ-law @ 8 kHz, decodes them with the codec in `audio.py`, and upsamples to PCM16 16 kHz.
3. Decoded frames are fed to the SAA gate via `attenlabs-saa`'s `feed_audio()`. The `AttentionClient` is constructed with `enable_audio=False, enable_video=False`, so the SDK never opens the host's microphone or camera; Twilio audio is the only source.
4. `@saa.on_turn_ready` fires when SAA has collected a complete, device-directed utterance. The adapter dispatches it to your `Bridge.on_speech()` implementation for STT / LLM / TTS.
5. TTS audio from the bridge flows back through the adapter's paced outbound sender (20 ms cadence) as μ-law @ 8 kHz `media` events.
6. `mark_responding(True/False)` is driven automatically when bytes start and stop flowing through the outbound queue, so SAA suppresses predictions during the agent's own TTS playback, preventing the carrier echo from re-triggering the gate.

## Samples

| Sample | Description | Run |
|---|---|---|
| [media_streams/](./media_streams) | Twilio Media Streams call, SAA-gated via `feed_audio` | `python -m uvicorn server:app --port 8765` |

## Quick start

Needs **Python 3.10 to 3.12** and a Twilio phone number.

```bash
git clone https://github.com/attenlabs/saa-sdk.git
cd saa-sdk/examples/twilio
cp .env.example .env     # fill SAA_API_KEY plus Twilio + bridge keys

cd media_streams
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ../../../packages/saa-py              # local dev against this repo
pip install -r requirements.txt
python -m uvicorn server:app --host 0.0.0.0 --port 8765
```

In a second terminal, expose the server via ngrok:

```bash
ngrok http 8765
```

Copy the ngrok hostname (no `https://` scheme) into `.env` as `PUBLIC_HOSTNAME`, then point your Twilio phone number's **Voice webhook** at `https://<PUBLIC_HOSTNAME>/voice`. Call the number and you will see SAA-gated `[bridge] turnReady` log lines when the caller speaks to the device.

## The lines that integrate SAA

```python
from saa import AttentionClient

saa = AttentionClient(
    token=os.environ["SAA_API_KEY"],
    enable_audio=False,   # no mic, Twilio audio fed manually
    enable_video=False,   # phone calls have no video track
    initial_threshold=float(os.environ.get("SAA_THRESHOLD", "0.7")),
)

@saa.on_turn_ready
def _on_turn_ready(event):
    # event.audio_pcm16 is a np.int16 array, PCM16 @ 16 kHz mono
    asyncio.run_coroutine_threadsafe(
        bridge.on_speech(event.audio_pcm16, event.duration_sec), loop
    )

# Feed each decoded 100 ms frame to SAA instead of sending from a mic:
saa.feed_audio(pcm16_16k_bytes)

# Tell SAA when the agent is speaking so it suppresses its own echo:
await session.mark_responding(True)   # before TTS bytes go out
await session.mark_responding(False)  # after the outbound queue drains
```

## Shared environment

One env file, [`.env.example`](./.env.example) in this directory:

| Variable | Purpose |
|---|---|
| `SAA_API_KEY` | SAA authentication token, get one at [attentionlabs.ai/dashboard](https://attentionlabs.ai/dashboard) |
| `SAA_THRESHOLD` | Gate sensitivity, 0..1 (default `0.7`) |
| `TWILIO_ACCOUNT_SID` | Twilio account SID, used for webhook signature validation |
| `TWILIO_AUTH_TOKEN` | Twilio auth token, required for `X-Twilio-Signature` validation and outbound REST calls |
| `TWILIO_FROM_NUMBER` | E.164 number for outbound calls placed via `outbound.py` |
| `PUBLIC_HOSTNAME` | ngrok host or load-balancer hostname **without** the `https://` scheme |
| `OPENAI_API_KEY` | Required for `OpenAIRealtimeBridge` |
| `DEEPGRAM_API_KEY` | Required for `DeepgramOpenAIElevenLabsBridge` |
| `ELEVENLABS_API_KEY` | Required for `DeepgramOpenAIElevenLabsBridge` |

## Requirements & limitations

- **Audio-only.** Phone calls have no video track. SAA runs in audio-only mode (`enable_video=False`), which means the visual signal that separates device-directed speech from talking to a person in the room is not available. Telephony is therefore a weaker showcase for SAA's device-directed classification than an in-room multimodal deployment (robot, kiosk, laptop).
- **Narrowband signal.** Twilio PSTN audio is μ-law G.711 @ 8 kHz, band-limited to ~3.4 kHz. SAA performs best on wideband audio, so narrowband telephony degrades classification confidence compared to a 16 kHz microphone feed.
- **Call quality dependence.** Reliability tracks carrier signal quality. A lossy or heavily compressed leg (VoIP -> PSTN handoff, weak cellular, echo-heavy room) degrades the PCM fed to SAA and can raise the false-reject rate.
- **End-of-turn latency.** The adapter accumulates Twilio's 20 ms inbound frames into 100 ms SAA frames before forwarding. SAA's turn accumulator adds additional latency; under continuous cross-talk very long utterances may be cut off at the maximum turn length.
- **Signature validation is optional in dev.** `X-Twilio-Signature` is validated only when `TWILIO_AUTH_TOKEN` is set. Always set it in production; an unsigned POST to `/voice` is enough for an attacker to redirect callers to a stream URL of their choice.

## Recommended usage

Try three send thresholds and keep the one that performs best: `0.5`, `0.7`, `0.8`.
Raise it for fewer false triggers, lower it to catch borderline speech. Set `SAA_THRESHOLD`, or call `set_threshold(v)` live.
