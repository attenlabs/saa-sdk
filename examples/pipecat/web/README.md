# web — all-in-one SAA + Daily browser demo

A single `uvicorn` process that creates an ephemeral Daily room, summons the hidden SAA agent, **spawns a Pipecat voice agent into the same room** (when provider keys are present), and serves a vanilla HTML/JS frontend that renders SAA's prediction stream as a live overlay. No build step, no framework, one terminal.

The address-decision flips the pill green only when you're talking *to* the device — and when the voice agent is enabled, it talks back only on green-pill turns.

This is the Daily/Pipecat sibling of [`examples/livekit/web`](../../livekit/web). Same UI, same SAA event shapes, different transport underneath.

## Files

| File | What it is |
|---|---|
| [`token_server.py`](./token_server.py) | FastAPI dev server. `/session` creates a Daily room, mints user + bot meeting tokens, summons the hidden SAA agent, and (if provider keys are present) spawns the voice agent in-process. Serves the static files too. |
| [`voice_agent.py`](./voice_agent.py) | The embedded Pipecat voice agent — `async run_voice_agent(...)` factored from [`voice_agent_cascaded`](../voice_agent_cascaded) so token_server.py can launch it per /session. |
| [`index.html`](./index.html) | UI shell (prediction pill, confidence bar, VAD/faces, video, mode badge). |
| [`app.js`](./app.js) | call-object Daily client, publishes tracks, consumes the `"saa"` app-message topic. |
| [`turn-parser.js`](./turn-parser.js) | decodes the binary turn payload (PCM16 + JPEGs). |
| [`styles.css`](./styles.css) | minimal styling. |

## Two modes — overlay only vs. talkback

The demo runs in one of two modes depending on what's in `.env`:

| Mode | Requires | What you get |
|---|---|---|
| **Talkback** | `SAA_API_KEY`, `DAILY_API_KEY`, `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY` | The voice agent joins your room and responds with TTS — but only when SAA says you're addressing the device. Side conversations are dropped before STT fires. |
| **Overlay only** | `SAA_API_KEY`, `DAILY_API_KEY` | The browser still renders SAA predictions live (use it to tune `vad_threshold` or watch class-1/class-2 transitions), but nothing talks back. |

The token_server logs which mode it's in on startup, and the UI's header shows it too once you click Start.

## Run

```bash
cd examples/pipecat/web

# Python 3.11+ is required: pipecat-ai 1.x dropped 3.10 support
python3.11 -m venv .venv && source .venv/bin/activate

# install the in-tree client FIRST so the requirements.txt version spec
# resolves locally — saa-pipecat-client is not on PyPI yet
pip install -e ../../../packages/saa-pipecat-client
pip install -r requirements.txt

cp .env.example .env   # fill in the keys — at minimum SAA_API_KEY + DAILY_API_KEY
set -a && source .env && set +a

uvicorn token_server:app --port 8000
# open http://localhost:8000 and click Start
```

The voice-agent track in `requirements.txt` (`pipecat-ai[daily,silero,deepgram,openai,cartesia]>=1.0.0,<2`) is the big install — if you only need the overlay path you can comment it out, but importing `voice_agent.py` will then fail loudly on startup. The simpler thing is to install everything once and let the runtime mode toggle decide whether to actually spawn the agent.

## How the overlay is wired

SAA events arrive as JSON on Daily's **app-message channel** under the `"saa"` topic. The integration surface in `app.js` is two functions, identical to the LiveKit demo:

- `renderPrediction(msg)` — reads `msg.aligned_class` (0/1/2), `msg.confidence`, `msg.num_faces`
- `renderVAD(msg)` — reads `msg.is_speech`

The bot publishes envelopes like `{topic:"saa", type:"prediction", ...}`. The Daily `app-message` event wraps that as `{ data, fromId }`, so `app.js` destructures `data` and filters on `data.topic === "saa"`.

### turn_ready chunk reassembly

Daily has no byte-stream primitive, so the per-turn binary blob (PCM16 + JPEGs, LiveKit-identical layout) is base64-encoded and split across multiple app messages — see the [`daily-integration.md` spec](../../../docs/) for the wire shape. `app.js` keeps a small `pending` map keyed on `stream_id`:

1. A `turn_ready` (or `interjection`) envelope arrives with `total_chunks` + `byte_len` — start an empty buffer.
2. Each subsequent `turn_chunk` carries a base64 slice + `index`; store it.
3. Once all chunks are gathered, concat them, call `parseTurnPayload(buf)`, and log the result.

The reassembly map is capped at 10 in-flight streams; oldest is dropped on overflow.

## How the voice agent is wired

`voice_agent.py` is the cascaded Pipecat agent ([`voice_agent_cascaded`](../voice_agent_cascaded) is the standalone reference). When `/session` fires, token_server.py mints a Daily meeting token for the bot, hands it + the SAA hosted session's `agent_identity` to `run_voice_agent(...)`, and spawns the result as an asyncio task. The agent joins the room a beat after the human does, wires its own `AttentionEngine` against the same SAA bot the browser is listening to, and runs the Silero-VAD → Deepgram → OpenAI → Cartesia pipeline gated by SAA predictions.

Lifecycle: the agent shuts down on `on_participant_left` (when you click Stop in the browser). The SAA hosted session is owned by token_server.py — it stays alive until the broker reaps it on idle (~5 min).

## Production warning

`token_server.py` is **dev-only**: open CORS, creates a billed Daily room on every `/session` hit, starts a billed SAA session, and (in talkback mode) burns OpenAI/Deepgram/Cartesia tokens per turn. For production you need auth on `/session`, rate limiting, a real room/identity policy, and your customers should mint the hidden-bot token using *their* Daily API key — not ours. The SAA API key, the provider keys, and the Daily API key must always stay server-side; the browser only ever receives the Daily room URL, its own user meeting token, and the SAA agent identity.

For deploying the voice agent without the FastAPI scaffold (Daily Bots, Pipecat Cloud, Modal, k8s), use [`voice_agent_cascaded`](../voice_agent_cascaded) — same agent logic with a standalone `python src/agent.py` entrypoint and a `Dockerfile`.
