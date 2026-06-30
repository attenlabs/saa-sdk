# SAA + Vapi WebSocket transport

A sample that enables your [Vapi](https://docs.vapi.ai) assistant to only respond when people are talking to it, and stay silent to side conversations or background voices.

## How SAA integrates

Vapi's WebSocket transport exposes a raw PCM16 seam you own end to end, so this sample uses the streaming SDK's `feed_audio` ingestion:

1. `sounddevice` captures 16 kHz mono PCM from the local mic.
2. Every frame is teed into SAA via [`attenlabs-saa`](../../packages/saa-py)'s `feed_audio()` (feed mode: `enable_audio=False`, the SDK captures nothing itself).
3. SAA classifies each frame and emits `prediction` / `turn_ready` / `interrupt` events.
4. The sample gates what reaches Vapi: real audio while device-directed, silence otherwise. Assistant playback drives `mark_responding()` so SAA knows when the agent is speaking.

## Samples

| Sample | Stack | Run |
|---|---|---|
| [`voice_agent/`](./voice_agent) | Vapi WebSocket transport + local mic/speaker, SAA-gated via `feed_audio` | `python agent.py` |

## Quick start

Needs **Python 3.10+**, a Vapi private API key, and an assistant ID from the [Vapi dashboard](https://dashboard.vapi.ai).

```bash
git clone https://github.com/attenlabs/saa-sdk.git
cd saa-sdk/examples/vapi
cp .env.example .env     # fill SAA_API_KEY, VAPI_API_KEY, VAPI_ASSISTANT_ID

cd voice_agent
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ../../../packages/saa-py              # local dev against this repo
pip install -r requirements.txt
python agent.py
```

The sample **auto-loads** `examples/vapi/.env`.

## The lines that integrate SAA

```python
saa = AttentionClient(token=SAA_API_KEY, enable_audio=False, enable_video=False)

@saa.on_prediction
def _(ev): gate.update_gate(ev.cls)            # only device-directed audio reaches Vapi

saa.start()
# mic tee -> saa.feed_audio() + gated PCM over the Vapi WebSocket
```

See [`voice_agent/README.md`](./voice_agent/README.md) for the full walk-through.

## Shared environment

One env file, [`.env`](./.env.example) in this directory:

| Key | Purpose |
|---|---|
| `SAA_API_KEY` | Your attention labs API key. Get one at [attentionlabs.ai/dashboard](https://attentionlabs.ai/dashboard). |
| `VAPI_API_KEY` | Vapi private API key |
| `VAPI_ASSISTANT_ID` | The assistant to run |

## Requirements & limitations

- **Audio-only** — no video track on the WebSocket transport.
- Turn boundaries are Vapi's STT/VAD on the gated stream (same tradeoff as the ElevenLabs sample).
- Requires PortAudio for local mic + speaker (`sounddevice`).
