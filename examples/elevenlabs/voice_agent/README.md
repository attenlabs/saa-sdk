# voice_agent — SAA-gated ElevenLabs Conversational AI

An [ElevenLabs Conversational AI](https://elevenlabs.io/docs/eleven-agents/overview) agent with **Attention Labs SAA** addressee gating wired on top, via the streaming SDK's `feed_audio` ingestion.

## The integration

Single file ([`agent.py`](./agent.py)). The moving parts:

- `AttentionClient(token=..., enable_audio=False, enable_video=False)` -> streaming SDK in **feed mode**: it opens the cloud WebSocket but captures nothing itself.
- `SAAFeedAudioInterface(DefaultAudioInterface(), saa)` -> wraps ElevenLabs' audio interface. Its mic tee feeds **every** frame to SAA, and sends ElevenLabs a **continuous stream**: the user's real audio while SAA says device-directed, **silence** otherwise (non-addressed speech, or while the agent is speaking — so its own echo never loops back). ElevenLabs keeps doing its own VAD/endpointing on that stream; SAA just decides what it hears.
- `@saa.on_prediction` → `attn.update_gate(ev.cls == 2)` -> **the gate**, with a **close-debounce**: it opens on class-2 but closes only after a short streak of non-class-2 ticks (default 4 ≈ 1 s). That stops a single class-0 dip from chopping an utterance, and hands ElevenLabs a real trailing-silence tail — the end-of-turn cue it needs to reply.
- `output()` / `interrupt()` → `saa.mark_responding(True/False)` — so SAA knows when the agent itself is speaking. `responding` is held for the agent TTS's **playback duration** (derived from the queued PCM bytes, +a short tail), because `DefaultAudioInterface` queues `output()` instantly but plays on a background thread; tracking `output()` idle instead would drop `responding` mid-playback and the agent's own echo would leak back.

### Warmup-gated greeting

SAA's model isn't classifying for real until its inference buffer fills (~10–15 s of audio). If the agent greeted immediately, it would speak into a cold classifier and the gate would be unreliable on the user's first reply. So:

- `attn.prime()` starts the mic feeding SAA *before* the ElevenLabs session connects — SAA warms up on real audio while the agent stays silent (gate closed, nothing forwarded yet).
- `@saa.on_warmup_complete` is SAA's **native** "warmed up + predicting" signal (first real prediction). The agent's `start_session()` (which triggers the greeting) is held until it fires — with a 20 s timeout fallback so a SAA stall still lets the agent greet.

The payoff: the agent greets only once SAA is live, and because SAA is already warm, the fail-closed gate works correctly from the very first user turn.

```python
saa = AttentionClient(token=SAA_API_KEY, enable_audio=False, enable_video=False)
attn = SAAFeedAudioInterface(DefaultAudioInterface(), saa, gate=True)

warmed = threading.Event()

@saa.on_warmup_complete
def _(): warmed.set()                             # native SAA warmup pivot

@saa.on_prediction
def _(ev): attn.set_gate_open(ev.cls == 2)        # the gate

conversation = Conversation(client=ElevenLabs(...), agent_id=..., requires_auth=True, audio_interface=attn)

saa.start()
attn.prime()                                      # warm SAA on real audio first
warmed.wait(timeout=20.0)
conversation.start_session()                      # greet into an already-warm SAA
```

## Quickstart

```bash
cd examples/elevenlabs
cp .env.example .env     # fill SAA_API_KEY, ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID

cd voice_agent
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ../../../packages/saa-py              # local dev against this repo
pip install -r requirements.txt
python agent.py
```

Talk to it. The agent answers only when you're addressing it; speech you direct at another person in the room never reaches the model.

## Terminal dashboard

On a TTY the agent renders a small live status frame ([`tui.py`](./tui.py)) once SAA warms up:
the current prediction + confidence (MODE), a rolling class buffer (BUFFER), the gate (GATE
OPEN/CLOSED), and the agent state (AGENT idle / listening / speaking). Without a TTY (piped, CI)
it no-ops, so the agent still runs headless.

## Cost note

SAA streaming is billed per session-minute; the ElevenLabs agent is billed by ElevenLabs.
