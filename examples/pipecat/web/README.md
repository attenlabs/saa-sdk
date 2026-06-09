# web — all-in-one SAA + Daily browser demo

A single `uvicorn` process that creates an ephemeral Daily room, summons the hidden SAA agent, **spawns an OpenAI Realtime voice agent into the same room** (when `OPENAI_API_KEY` is set), and serves a vanilla HTML/JS frontend that renders SAA's prediction stream as a live overlay. No build step, no framework, one terminal, one voice-agent key.

The address-decision flips the pill green only when you're talking *to* the device — and when the voice agent is enabled, it talks back only on green-pill turns.

This is the Daily/Pipecat sibling of [`examples/livekit/web`](../../livekit/web). Same UI, same SAA event shapes, different transport underneath.

## Files

| File | What it is |
|---|---|
| [`token_server.py`](./token_server.py) | FastAPI dev server. `/session` creates a Daily room, mints user + bot meeting tokens, summons the hidden SAA agent, and (if `OPENAI_API_KEY` is set) spawns the voice agent in-process. Serves the static files too. |
| [`voice_agent.py`](./voice_agent.py) | OpenAI Realtime voice agent (speech-to-speech). SAA gating, barge-in, and interjection wiring. |
| [`index.html`](./index.html) | UI shell (prediction pill, confidence bar, VAD/faces, video, mode badge). |
| [`app.js`](./app.js) | call-object Daily client, publishes tracks, consumes the `"saa"` app-message topic. |
| [`turn-parser.js`](./turn-parser.js) | decodes the binary turn payload (PCM16 + JPEGs). |
| [`styles.css`](./styles.css) | minimal styling. |

## Two modes — overlay only vs. talkback

The demo runs in one of two modes depending on what's in `.env`:

| Mode | Requires | What you get |
|---|---|---|
| **Talkback** | `SAA_API_KEY`, `DAILY_API_KEY`, **`OPENAI_API_KEY`** | OpenAI Realtime joins your room and responds via speech-to-speech but only when SAA says you're addressing the device. |
| **Overlay only** | `SAA_API_KEY`, `DAILY_API_KEY` only | Browser renders SAA predictions live (use it to tune `vad_threshold` or watch class-1 / class-2 transitions), but nothing talks back. |

The token_server logs which mode it's in on startup, and the UI's header shows it too once you click Start.

## Run

```bash
cd examples/pipecat/web

# Python 3.11+ is required (pipecat-ai 1.x dropped 3.10 support).
# macOS: brew install python@3.11
# Debian/Ubuntu/WSL2: sudo apt-get install python3.11 python3.11-venv
python3.11 -m venv .venv && source .venv/bin/activate

# install the in-tree client FIRST so the requirements.txt version spec
# resolves locally — saa-pipecat-client is not on PyPI yet
pip install -e ../../../packages/saa-pipecat-client
pip install -r requirements.txt

cp .env.example .env   # fill in the keys — at minimum SAA_API_KEY + DAILY_API_KEY
# Export the .env to your shell. Bash / Zsh (macOS / Linux / WSL2):
set -a && source .env && set +a
# Fish: `set -gx (cat .env | grep -v '^#' | string split0)` or use `direnv`.

uvicorn token_server:app --port 8000
# open http://localhost:8000 and click Start
```

The voice-agent dependency in `requirements.txt` is `pipecat-ai[daily,openai]`

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

`voice_agent.py` runs the Realtime LLM service ([`OpenAIRealtimeLLMService`](https://docs.pipecat.ai/server/services/llm/openai-realtime)) directly in the Pipecat pipeline. When `/session` fires, token_server.py mints a Daily meeting token for the bot, hands it + the SAA hosted session's `agent_identity` to `run_voice_agent(...)`, and spawns the result as an asyncio task. The agent joins the room a beat after the human does, wires its own `AttentionEngine` against the same SAA bot the browser is listening to, and runs:

```
transport.input() → AddresseeGate → OpenAIRealtimeLLMService → BotSpeakingObserver → transport.output()
```

- **AddresseeGate** drops `InputAudioRawFrame`s when SAA says `aligned_class == 1` and confidence is high — that audio never reaches OpenAI.
- **BotSpeakingObserver** watches `TTSStartedFrame` / `TTSStoppedFrame` (Realtime emits both on its way out of the service) and toggles `engine.responding_start()` / `responding_stop()` so SAA's interrupt detector arms only during playback.
- **Interrupt** queues `InterruptionTaskFrame` to cancel the in-flight Realtime turn on a confident barge-in.
- **Interjection** queues `LLMMessagesAppendFrame(messages=[...], run_llm=True)` so Realtime injects the system nudge and runs the model without waiting for further user audio.

Lifecycle: the agent shuts down on `on_participant_left` (when you click Stop in the browser). The SAA hosted session is owned by token_server.py — it stays alive until the broker reaps it on idle (~5 min).

## Production warning

`token_server.py` is **dev-only**: open CORS, creates a billed Daily room on every `/session` hit, starts a billed SAA session, and (in talkback mode) burns OpenAI Realtime audio-seconds per turn — which is **noticeably more expensive than cascaded** (no Deepgram/Cartesia underbid). For production you need auth on `/session`, rate limiting, a real room/identity policy, and your customers should mint the hidden-bot token using *their* Daily API key — not ours. The SAA API key, the OpenAI key, and the Daily API key must always stay server-side; the browser only ever receives the Daily room URL, its own user meeting token, and the SAA agent identity.

For deploying without the FastAPI scaffold (Daily Bots, Pipecat Cloud, Modal, k8s), use [`voice_agent_cascaded`](../voice_agent_cascaded) — same SAA integration shape with the cheaper cascaded pipeline, standalone `python src/agent.py` entrypoint, and a `Dockerfile`.
