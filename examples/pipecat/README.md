# SAA + Pipecat (on Daily)

A sample that adds **Attention Labs SAA** addressee gating to a [Pipecat](https://github.com/pipecat-ai/pipecat) voice agent running on Daily. SAA decides, per utterance, whether speech in the room was meant for the agent or not.

The [`web/`](./web) demo is a single `uvicorn` process that creates an ephemeral Daily room, summons the SAA agent, spawns an OpenAI Realtime voice agent into the same room (when `OPENAI_API_KEY` is set), and serves a vanilla HTML/JS frontend that renders SAA's prediction stream as a live overlay.

When the layout turns green, that means after the user's utterance the voice agent will respond.

## How SAA integrates

1. Your bot calls `start_attention_session(...)` to start a SAA session for your Daily room.
2. SAA joins your Daily room, subscribes to the user's audio and video, and runs the classifier.
3. It publishes events (`prediction`, `vad`, `turn_ready`, `interrupt`, `interjection`) on the `"saa"` Daily app-message topic.
4. Your bot consumes them via `AttentionEngine`, which hooks `@transport.event_handler("on_app_message")` on your `DailyTransport`.
5. Upstream actions (`responding_start`, `set_threshold`, ...) queue a `DailyOutputTransportMessageUrgentFrame` onto your bound `PipelineTask`.

The client ([`packages/saa-pipecat-client`](../../packages/saa-pipecat-client)) is pure Python.

## Prerequisites

### Supported platforms

| Platform | Status | Notes |
|---|---|---|
| **macOS** (arm64 + x86_64) | supported | daily-python ships wheels for both architectures. |
| **Linux** (x86_64 + aarch64) | supported | daily-python wheels are built against manylinux_2_28. |
| **Windows** (native) | not supported | daily-python publishes no Windows wheels and no source distribution. Use **WSL2** and follow the Linux instructions inside your WSL shell. |

### Python 3.11+

pipecat-ai 1.x dropped Python 3.10 support, so 3.11+ is required. `pip install` on 3.10 fails with `Package 'saa-pipecat-client' requires a different Python: 3.10.x not in '>=3.11'`.

| Platform | Install Python 3.11 |
|---|---|
| macOS (Homebrew) | `brew install python@3.11` |
| Debian / Ubuntu | `sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv` |
| Fedora / RHEL | `sudo dnf install -y python3.11` |
| Arch | `sudo pacman -S python311` (AUR; or use `pyenv install 3.11`) |
| WSL2 (Ubuntu) | Same as Debian / Ubuntu, inside the WSL shell, not PowerShell. |
| Any platform | [`pyenv install 3.11`](https://github.com/pyenv/pyenv) is the framework-agnostic option. |

## Environment

```
SAA_API_KEY=                 # Attention Labs API key (shared with the LiveKit samples)
DAILY_API_KEY=               # Daily.co REST key from dashboard.daily.co
OPENAI_API_KEY=              # optional: enables the talkback voice agent
```

## Run

```bash
cd examples/pipecat/web

# Python 3.11+ is required (pipecat-ai 1.x dropped 3.10 support).
# macOS: brew install python@3.11
# Debian/Ubuntu/WSL2: sudo apt-get install python3.11 python3.11-venv
python3.11 -m venv .venv && source .venv/bin/activate

# install the in-tree client FIRST so the requirements.txt version spec
# resolves locally; saa-pipecat-client is not on PyPI yet
pip install -e ../../../packages/saa-pipecat-client
pip install -r requirements.txt

cp .env.example .env   # fill in the keys: at minimum SAA_API_KEY + DAILY_API_KEY

python -m uvicorn token_server:app --port 8000
# open http://localhost:8000 and click Start
```

The voice-agent dependency in `requirements.txt` is `pipecat-ai[daily,openai]`.

## Two modes: overlay only vs. talkback

The demo runs in one of two modes depending on what's in `.env`:

| Mode | Requires | What you get |
|---|---|---|
| **Talkback** | `SAA_API_KEY`, `DAILY_API_KEY`, **`OPENAI_API_KEY`** | OpenAI Realtime joins your room and responds via speech-to-speech, but only when SAA says you're addressing the device. |
| **Overlay only** | `SAA_API_KEY`, `DAILY_API_KEY` only | Browser renders SAA predictions live (use it to tune `vad_threshold` or watch class-1 / class-2 transitions), but nothing talks back. |

The token_server logs which mode it's in on startup, and the UI's header shows it too once you click Start.

## The integration code

```python
session = await start_attention_session(
    api_key=SAA_API_KEY, room_url=ROOM_URL,
    agent_token=attention_agent_token(daily_api_key=DAILY_API_KEY, room_name=room_name),
    participant_identity=human_identity,
)
engine = AttentionEngine(transport, agent_identity=session.agent_identity)
engine.bind_task(task)

@engine.on_prediction
def _(p): addressee_gate.suppressed = (p.aligned_class == 1 and p.confidence > 0.7)  # the gate

@engine.on_interrupt
async def _(ev): await task.queue_frames([InterruptionTaskFrame()])                  # barge-in

@engine.on_interjection
async def _(ev): await task.queue_frames([LLMMessagesAppendFrame(messages=[...], run_llm=True)])
```

Plus a `BotSpeakingObserver` FrameProcessor that watches `TTSStartedFrame` / `TTSStoppedFrame` and calls `engine.responding_start()` / `responding_stop()` so SAA knows when your agent is the one speaking. This is required for interrupt and interjection to fire correctly.

## How the overlay is wired

SAA events arrive as JSON on Daily's app-message channel under the `"saa"` topic. The integration surface in `app.js` is two functions, identical to the LiveKit demo:

- `renderPrediction(msg)` reads `msg.aligned_class` (0/1/2), `msg.confidence`, `msg.num_faces`.
- `renderVAD(msg)` reads `msg.is_speech`.

The bot publishes envelopes like `{topic:"saa", type:"prediction", ...}`. The Daily `app-message` event wraps that as `{ data, fromId }`, so `app.js` destructures `data` and filters on `data.topic === "saa"`.

### turn_ready chunk reassembly

Daily has no byte-stream primitive, so the per-turn binary blob (PCM16 + JPEGs) is base64-encoded and split across multiple app messages. `app.js` keeps a small `pending` map keyed on `stream_id`:

1. A `turn_ready` (or `interjection`) envelope arrives with `total_chunks` + `byte_len`; start an empty buffer.
2. Each subsequent `turn_chunk` carries a base64 slice + `index`; store it.
3. Once all chunks are gathered, concat them, call `parseTurnPayload(buf)`, and log the result.

The reassembly map is capped at 10 in-flight streams; the oldest is dropped on overflow.

## How the voice agent is wired

`voice_agent.py` runs the Realtime LLM service ([`OpenAIRealtimeLLMService`](https://docs.pipecat.ai/server/services/llm/openai-realtime)) directly in the Pipecat pipeline. When `/session` fires, token_server.py mints a Daily meeting token for the bot, hands it plus the SAA session's `agent_identity` to `run_voice_agent(...)`, and spawns the result as an asyncio task. The agent joins the room a beat after the human does, wires its own `AttentionEngine` against the same SAA session the browser is listening to, and runs:

```
transport.input() -> AddresseeGate -> OpenAIRealtimeLLMService -> BotSpeakingObserver -> transport.output()
```

- **AddresseeGate** drops `InputAudioRawFrame`s when SAA says `aligned_class == 1` and confidence is high; that audio never reaches OpenAI.
- **BotSpeakingObserver** watches `TTSStartedFrame` / `TTSStoppedFrame` and toggles `engine.responding_start()` / `responding_stop()` so SAA's interrupt detector arms only during playback.
- **Interrupt** queues `InterruptionTaskFrame` to cancel the in-flight Realtime turn on a confident barge-in.
- **Interjection** queues `LLMMessagesAppendFrame(messages=[...], run_llm=True)` so Realtime injects the system nudge and runs the model without waiting for further user audio.

Lifecycle: the agent shuts down on `on_participant_left` (when you click Stop in the browser). The SAA session is owned by token_server.py and stays alive until it is reaped on idle (~5 min).

## Requirements and limitations

- The Daily room must be reachable from the SAA cloud (Daily Cloud rooms are public by default).
- Both audio and video tracks should be available.
- One target participant per session. Multi-user rooms need one `start_attention_session` call each.
- `DailyParams(audio_in_user_tracks=True)` is required when your bot shares the room with the human, otherwise the bot's own TTS feeds back as `InputAudioRawFrame`s.
- Identity matching uses the nested `participant["info"]["userName"]`, not the top-level `userName`.

## Deploy targets

The same agent code runs on Pipecat Cloud, Modal, k8s, or your own VM, anywhere `pipecat-ai[daily]` runs. Put the Daily REST key (from `dashboard.daily.co -> Developers`, or the Pipecat Cloud Settings -> Daily (WebRTC) tab) in `DAILY_API_KEY`.

## Production warning

`token_server.py` is **dev-only**: open CORS, creates a billed Daily room on every `/session` hit, starts a billed SAA session, and in talkback mode burns OpenAI Realtime audio-seconds per turn. For production you need auth on `/session`, rate limiting, a real room/identity policy, and your customers should mint the bot token using *their* Daily API key, not yours. The SAA API key, the OpenAI key, and the Daily API key must always stay server-side; the browser only ever receives the Daily room URL, its own user meeting token, and the SAA agent identity.
