# SAA + Twilio Media Streams — `media_streams/`

`server.py` is the bidirectional Twilio Media Streams adapter. It exposes:

| Route | Method | Purpose |
|-------|--------|---------|
| `/voice` | POST | TwiML webhook — returns `<Connect><Stream>` pointing at `/twilio` |
| `/voice/outbound` | POST | TwiML for outbound calls placed via `outbound.py` |
| `/twilio` | WebSocket | Bidirectional Media Streams handler — SAA gating lives here |
| `/twilio-status` | POST | Call-status callbacks (initiated / ringing / answered / completed) |
| `/health` | GET | Liveness probe — returns `ok` |
| `/ready` | GET | Readiness probe — 200 once `SAA_API_KEY` is set, 503 otherwise |
| `/stats` | GET | Prometheus-shaped aggregate counters (calls, audio bytes, barge-ins) |

## Bridges

| Bridge | File | Extra deps |
|--------|------|------------|
| `LoggingBridge` | `bridge.py` | none — good for smoke-testing the adapter |
| `OpenAIRealtimeBridge` | `bridge_openai_realtime.py` | `websockets>=12`, `openai>=1.0` |
| `DeepgramOpenAIElevenLabsBridge` | `bridge_deepgram_openai_elevenlabs.py` | `openai>=1.0`, `deepgram-sdk>=3`, `elevenlabs>=1.0` |

The default is `LoggingBridge`. To swap in a different bridge, call
`set_bridge_factory` at startup before uvicorn starts serving:

```python
from server import app, set_bridge_factory
from bridge_openai_realtime import OpenAIRealtimeBridge
import os

set_bridge_factory(lambda: OpenAIRealtimeBridge(api_key=os.environ["OPENAI_API_KEY"]))
```

## How to run

```bash
cd examples/twilio
cp .env.example .env          # fill SAA_API_KEY and optionally Twilio + bridge keys

cd media_streams
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ../../../packages/saa-py             # local repo dev
pip install -r requirements.txt
python -m uvicorn server:app --host 0.0.0.0 --port 8765
```

## Wiring ngrok

Twilio needs a publicly reachable URL for the `/voice` webhook and for the
`/twilio` WebSocket. Use ngrok in a second terminal:

```bash
ngrok http 8765
```

Copy the `https://…ngrok-free.app` hostname (without the `https://` scheme)
into your `.env`:

```
PUBLIC_HOSTNAME=abc123.ngrok-free.app
```

Then set your Twilio phone number's **Voice webhook** to:

```
https://abc123.ngrok-free.app/voice
```

Restart `server.py` after changing `.env`. Calls to the Twilio number now
flow through the adapter.

## Outbound calls

```bash
python -m outbound +15551112222
```

Requires `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, and
`PUBLIC_HOSTNAME` in `.env` or the environment. The adapter must already be
running.
