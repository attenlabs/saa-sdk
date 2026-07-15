# voice_agent_factory: SAA-gated LiveKit agent, greenfield factory

The shortest path to a device-directed voice agent: `build_attention_entrypoint` wires token + hosted session + `AttentionEngine` for you, so all you supply is a `handle_turn(ev, ctx)` callback. This one is log-only, forward `ev.audio_pcm16` to your own STT/LLM/TTS.

## Setup

Uses the shared [`examples/livekit/.env`](../.env): `SAA_API_KEY`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`.

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate

pip install -r requirements.txt
pip install -e ../../../packages/saa-livekit-client   # local dev against this repo

python agent.py dev
```

Connect a frontend (the [`web`](../web) sample, or the [LiveKit Agents Playground](https://agents-playground.livekit.io)) and talk.
