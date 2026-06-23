# SAA + Twilio Media Streams: `media_streams/`

`server.py` is the bidirectional Twilio Media Streams adapter. It exposes:

| Route | Method | Purpose |
|---|---|---|
| `/voice` | POST | TwiML webhook. Returns `<Connect><Stream>` pointing at `/twilio` |
| `/voice/outbound` | POST | TwiML for outbound calls placed via `outbound.py` |
| `/twilio` | WebSocket | Bidirectional Media Streams handler. SAA gating lives here |
| `/twilio-status` | POST | Call-status callbacks (initiated / ringing / answered / completed) |
| `/health` | GET | Liveness probe. Returns `ok` |
| `/ready` | GET | Readiness probe. 200 once `SAA_API_KEY` is set, 503 otherwise |
| `/stats` | GET | Aggregate counters as JSON (calls, audio bytes, barge-ins) |
| `/metrics` | GET | Same aggregate counters as `/stats` in Prometheus text exposition format |

## Bridges

| Bridge | File | Extra deps |
|---|---|---|
| `LoggingBridge` | `bridge.py` | none, good for smoke-testing the adapter |
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
git clone https://github.com/attenlabs/saa-sdk.git
cd saa-sdk/examples/twilio
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

Copy the `https://<id>.ngrok-free.app` hostname (without the `https://` scheme)
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

## Hardening / cost control

A live phone call bills per minute on both SAA and Twilio, so a stuck or
abandoned stream is a money leak. The adapter ships several optional ceilings,
all configured via the environment (see `.env.example`). They default to
**off/permissive** so a fresh checkout behaves exactly as before; opt in for
production:

| Env var | Default | Effect |
|---|---|---|
| `MAX_CALL_DURATION_SECONDS` | `0` (off) | Force-disconnect a call past this wall-clock duration. `1800` (30 min) recommended for production. |
| `IDLE_HANGUP_SECONDS` | `0` (off) | Hang up after this long with no inbound media. |
| `MAX_CONCURRENT_CALLS` | `0` (off) | Reject new inbound calls once this many are active (`1013 Try Again Later`). |
| `TWILIO_WS_IDLE_TIMEOUT` | `60` (on) | Close the Twilio WebSocket if no frame arrives for this long. A live call sends media every 20 ms, so this only reaps dead/half-open sockets. |
| `BRIDGE_OPEN_TIMEOUT` | `10.0` (on) | Tear the call down if the bridge's `open()` (which may dial an upstream LLM) hangs past this. |
| `REQUIRE_TWILIO_SIGNATURE` | unset (off) | Fail closed if a request arrives without a valid `X-Twilio-Signature`. Set to `1` **and** set `TWILIO_AUTH_TOKEN` in production. |

The two on-by-default timeouts (`TWILIO_WS_IDLE_TIMEOUT`, `BRIDGE_OPEN_TIMEOUT`)
only reap zombie/stalled calls; they never cut a call that is actively
streaming. Set any knob to `0` to disable it.

The `OpenAIRealtimeBridge` additionally reconnects to the OpenAI Realtime API
with capped exponential backoff if the socket drops mid-call or a connect is
refused with `429` (rate limit) / `529` (overloaded). Tune via
`OPENAI_REALTIME_RECONNECT_ATTEMPTS`, `OPENAI_REALTIME_RECONNECT_BASE_DELAY`,
and `OPENAI_REALTIME_RECONNECT_MAX_DELAY`.
