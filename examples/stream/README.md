# SAA + Stream (getstream.io)

Integrates the **Selective Auditory Attention (SAA) SDK** with [Stream](https://getstream.io/) (getstream.io) for live video calls.

SAA predicts, on every frame, whether the people in the room are talking **to the device** (class 2) or **to each other** (class 1). This example shows that prediction running live inside a real Stream call.

## Tech stack

| Layer | Technology |
|---|---|
| Token server | Python · FastAPI · `getstream` Python SDK |
| SAA | `@attenlabs/saa-js` — runs in the browser, not server-side |
| Calling | `@stream-io/video-client` via `esm.sh` importmap |
| Voice AI (optional) | OpenAI Realtime API — browser WebSocket |

## Architecture

```
Browser
├── AttentionClient (@attenlabs/saa-js)
│     prediction / turnReady / vad / interrupt events
└── StreamVideoClient (@stream-io/video-client)
      WebRTC → Stream SFU → other participants
         ↓
         optional: OpenAI Realtime (voice AI, browser-side)

Server (FastAPI · token_server.py)
└── getstream SDK → mints JWT + creates call → returned to browser
```

Unlike LiveKit and Pipecat (where a server-side agent joins the room), Stream Video has no Python media-participant SDK. Both SAA and the optional LLM bridge run in the browser, consistent with `examples/web/`.

**SAA as a gate:** only `turnReady` (class 2 = device-directed) utterances reach the LLM. Human side-talk is filtered before any inference call is made.

## Example

| Example | Call type | SAA mode | Use case |
|---------|-----------|----------|----------|
| [web/](web/) | `default` (video) | multimodal | Video calls with attention overlay |

## Quick start

```bash
cd examples/stream/web
pip install -r requirements.txt && npm install
cp ../.env.example .env   # fill in STREAM_API_KEY, STREAM_API_SECRET, SAA_API_KEY
uvicorn token_server:app --port 8000
```

See [web/README.md](web/README.md) for full setup details.
