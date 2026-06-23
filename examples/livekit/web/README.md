# web: browser front-end for a SAA-gated LiveKit voice agent

A vanilla HTML/JS client that joins a LiveKit room, publishes cam+mic, plays the
agent's voice, and renders SAA's gating decisions as a live overlay.

When the pill turns green, the agent will respond to what you just said.

> **Requires a voice agent.** It needs one of the voice-agent workers running in
> the **same LiveKit project**. That worker owns the SAA session and answers you:
> [`voice_agent_realtime`](../voice_agent_realtime). Without an agent in the room
> there is no SAA session and nothing to talk to.

## Files

| File | What it is |
|---|---|
| [`token_server.py`](./token_server.py) | tiny FastAPI endpoint, mints a browser LiveKit join token and serves the static files. No SAA here. |
| [`index.html`](./index.html) | UI shell (prediction pill, confidence bar, VAD/faces, video). |
| [`app.js`](./app.js) | connects to the room, publishes tracks, plays the agent's audio, renders the `"saa"` overlay. |
| [`turn-parser.js`](./turn-parser.js) | decodes the binary turn payload (PCM16 + JPEGs). |
| [`styles.css`](./styles.css) | minimal styling. |

## Run

Both halves read the shared [`examples/livekit/.env`](../.env.example) (auto-loaded, 
no `source` step, identical on Windows/macOS/Linux), so `LIVEKIT_*` is identical by
construction. Fill it in once: `cd examples/livekit && cp .env.example .env`.

**Terminal 1, a voice agent** (owns SAA, auto-dispatches into new rooms):

```bash
cd ../voice_agent_realtime
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt && pip install -e ../../../packages/saa-livekit-client
python agent.py dev
```

**Terminal 2, this token server:**

```bash
git clone https://github.com/attenlabs/saa-sdk.git
cd saa-sdk/examples/livekit/web
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn token_server:app --port 8000
# open http://localhost:8000 and click Start
```

Start the agent **first** so it's registered to receive the room dispatch. When
you click Start, the browser creates a room, the agent auto-joins and summons SAA.
The pill goes green when you're talking to the device.

## How the overlay is wired

SAA events arrive as JSON on the `"saa"` data topic, published by SAA, which the
*voice agent* summoned. The integration surface is two functions in `app.js`:

- `renderPrediction(msg)`, reads `msg.aligned_class` (0/1/2), `msg.confidence`, `msg.num_faces`
- `renderVAD(msg)`, reads `msg.is_speech`

`turn_ready` / `interjection` arrive as a JSON envelope plus a binary byte stream
on the same topic; `onByteStream` reassembles it and `parseTurnPayload` decodes
the PCM + frames.

## Production warning

`token_server.py` is **dev-only**: open CORS, mints a token for any
`room`/`identity`. For production you need auth on `/token`, rate limiting, and a
real room/identity policy. LiveKit API secrets must stay server-side, the
browser only ever receives the join token.
