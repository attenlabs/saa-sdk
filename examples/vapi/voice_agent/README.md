# voice_agent: SAA-gated Vapi WebSocket assistant

A [Vapi](https://docs.vapi.ai) assistant reached over the **WebSocket transport**, with **attention labs SAA** device-directed gating wired on top via the streaming SDK's `feed_audio` ingestion.

## The integration

Single file ([`agent.py`](./agent.py)). The moving parts:

- `AttentionClient(token=..., enable_audio=False, enable_video=False)` → streaming SDK in **feed mode**: it opens the cloud WebSocket but captures nothing itself.
- `sounddevice` captures 16 kHz mono PCM from the local mic; every frame is teed into `saa.feed_audio()`.
- `@saa.on_prediction` → `gate.update(ev.cls)` → **the gate**: opens on class-2 (device-directed), closes immediately on class-1 (human-directed). Vapi receives the user's real audio while the gate is open and silence otherwise, so side talk never reaches the assistant's STT.
- Vapi assistant audio on the return WebSocket drives `saa.mark_responding(True/False)` so SAA knows when the agent is speaking (echo is not fed back as user speech).

### Warmup-gated session start

SAA's model isn't classifying for real until its inference buffer fills (~10–15 s of audio). The mic starts feeding SAA before the Vapi call is created; `start_session()` (REST + WebSocket dial) is held until `@saa.on_warmup_complete` fires, with a 20 s timeout fallback.

## Quickstart

```bash
git clone https://github.com/attenlabs/saa-sdk.git
cd saa-sdk/examples/vapi/voice_agent
cp ../.env.example ../.env     # fill SAA_API_KEY, VAPI_API_KEY, VAPI_ASSISTANT_ID

python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ../../../packages/saa-py              # local dev against this repo
pip install -r requirements.txt
python agent.py
```

Talk to it. The assistant answers only when you're addressing it; speech directed at someone else in the room is replaced with silence before it reaches Vapi.

## Logs

One line per SAA prediction so the gating is observable:

```
PRED cls=2 conf=0.91 src=model | resp=False gate=open send=real
```

`cls` is the prediction (0 silent / 1 human-directed / 2 device-directed), `gate` the debounced gate, and `send` whether real audio or silence (`muted`) is reaching Vapi that tick.

## Requirements & limitations

- **Audio-only** — Vapi WebSocket transport carries no video, so class-1 ("talking to a human") is weaker than on the multimodal LiveKit / Pipecat paths.
- Needs a Vapi **assistant** configured in the dashboard; this sample does not create one inline.
- Turn boundaries are Vapi's own STT/VAD on the gated audio stream (same tradeoff as the ElevenLabs sample).
- Requires PortAudio (`sounddevice`) for local mic + speaker access.
