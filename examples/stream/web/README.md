# SAA + Stream Video — web demo

A browser client that runs SAA attention prediction inside a live [Stream Video](https://getstream.io/video/) call. A FastAPI token server mints Stream credentials and creates the call; the browser joins, starts SAA, and (optionally) routes detected device-directed turns to OpenAI Realtime.

## Tech stack

| Layer | Technology |
|---|---|
| Token server | Python · FastAPI · `getstream` SDK (mints JWT, creates call server-side) |
| SAA (browser) | `@attenlabs/saa-js` — `AttentionClient` |
| Video call (browser) | `@stream-io/video-client` via `esm.sh` importmap (no build step) |
| Voice AI (optional) | OpenAI Realtime API — browser-side WebSocket |

Both SAA and the call client run entirely in the browser. Stream Video has no Python media-participant SDK, so there is no server-side bot — the architecture mirrors `examples/web/` with Stream's SFU handling multi-participant WebRTC transport.

## How it works

1. **Start** — browser fetches `/session`: Stream user JWT + call ID generated server-side.
2. **SAA warmup** — `AttentionClient` captures mic + camera, warms up the model (~12 s). Prediction card shows indeterminate sweep during warmup.
3. **Stream call joined** — simultaneously with SAA startup; camera/mic published to Stream SFU.
4. **Live prediction** — per-frame: class 0 = silent, class 1 = human side-talk, class 2 = device-directed.
5. **Turn ready** — device-directed utterance forwarded to OpenAI Realtime (if `OPENAI_API_KEY` set); AI responds via audio.
6. **Stop** — leaves Stream call, stops SAA, tears down LLM bridge.

> SAA and Stream Video each capture the camera independently. Modern browsers share the physical device between both; no conflict occurs in practice.

## How SAA integrates with Stream

SAA and Stream run in parallel in the browser, sharing the same mic and camera:

```
mic + camera
     │
     ├──► AttentionClient (SAA) ──► SAA inference server
     │         │                           │
     │         │◄── predictions ◄──────────┘
     │         │
     │         ├── prediction  → update overlay (class 0/1/2 + confidence)
     │         ├── turnReady   → forward audio to OpenAI Realtime (if class 2)
     │         ├── interrupt   → cancel LLM output, unmute SAA
     │         └── vad         → mic activity indicator
     │
     └──► StreamVideoClient ──► Stream SFU (WebRTC)
               └── other participants receive your audio/video
```

**SAA event → action mapping:**

| Event | Trigger | Action |
|---|---|---|
| `connected` | SAA WebSocket open | join Stream call |
| `warmupComplete` | model ready (~12 s) | status → "live" |
| `prediction` | every frame | update prediction card (class, confidence, face count) |
| `vad` | mic activity | update VAD indicator |
| `turnReady` | device-directed utterance complete | send audio to OpenAI Realtime |
| `interrupt` | user spoke while AI was responding | cancel LLM, unmute SAA |

**The addressee gate in practice:**

Without SAA every utterance goes to the LLM. With SAA only `turnReady` events (class 2 = device-directed) reach OpenAI Realtime — side-talk between humans is filtered before any LLM call is made.

## Prerequisites

- [Stream account](https://dashboard.getstream.io/) — free plan sufficient
- [Attention Labs SAA token](https://attentionlabs.ai/dashboard/)
- Python 3.10+, Node.js 18+

## Setup

### 1. Install

```bash
cd examples/stream/web

# Python deps (includes getstream SDK for correct JWT minting)
pip install -r requirements.txt

# JS deps (saa-js via local import map; stream client via esm.sh)
npm install
```

### 2. Configure

```bash
cp ../.env.example .env
```

Edit `.env`:

```env
STREAM_API_KEY=your_stream_api_key        # Stream dashboard → App → API key
STREAM_API_SECRET=your_stream_api_secret  # Stream dashboard → App → Secret
SAA_API_KEY=your_saa_api_key              # attentionlabs.ai/dashboard
OPENAI_API_KEY=sk-...                     # optional — enables voice AI
```

Get Stream credentials at [dashboard.getstream.io](https://dashboard.getstream.io/) → your app → API & Authentication.

When `SAA_API_KEY` and `OPENAI_API_KEY` are set in `.env`, their input fields show **✓ configured via server .env** in green — you can click Start without entering anything.

### 3. Run

```bash
uvicorn token_server:app --port 8000
```

Open **http://localhost:8000** and click **Start**.

## Modes

| Mode | What you need | Behaviour |
|------|---------------|-----------|
| Overlay only | SAA token | Prediction overlay on Stream call; no AI response |
| Voice AI | SAA token + `OPENAI_API_KEY` | AI responds via speech when class 2 detected |

## Threshold tuning

| Setup | Try these |
|-------|-----------|
| With webcam | 0.60 · 0.77 · 0.88 |

Raise the threshold to reduce false triggers; lower it to catch quieter or off-axis speech. The slider at the bottom adjusts the threshold live.

## Architecture

```
Browser
├── AttentionClient (@attenlabs/saa-js)
│     on connected      → join Stream call
│     on warmupComplete → setStatus("live")
│     on prediction     → update prediction card + rolling buffer
│     on turnReady      → forward audio to RealtimeLLMBridge
│     on interrupt      → cancel LLM response
└── StreamVideoClient (@stream-io/video-client, via esm.sh)
      call.join({ create: true })
      call.camera.enable() / call.microphone.enable()
      call.state.participants$ → participant count

Server (FastAPI token_server.py)
  GET /config   → { saaConfigured, openaiConfigured }  ← no side effects
  GET /session  → { callId, userToken, streamApiKey, saaToken, openaiApiKey, … }
                  uses getstream Python SDK: upsert_users + call.get_or_create
  GET /*        → StaticFiles
```

## Files

| File | Purpose |
|------|---------|
| `token_server.py` | FastAPI: `/config`, `/session`, static files; uses `getstream` SDK |
| `requirements.txt` | Python deps (`getstream>=3.0.0`, `fastapi`, `uvicorn`, …) |
| `index.html` | Pipecat-style UI: config panel, prediction card, video preview, Start/Stop |
| `app.js` | Stream Video + SAA wiring |
| `styles.css` | Minimal dark theme matching other SAA examples |
| `llm.js` | OpenAI Realtime bridge (same as `examples/web/llm.js`) |
| `package.json` | npm: `@attenlabs/saa-js` |

> **Security:** `/session` returns the Stream API key and SAA token to the browser. Fine for local development; in production serve from an authenticated endpoint.
