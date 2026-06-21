# voice_agent_realtime: SAA-gated realtime LiveKit agent

A speech-to-speech voice agent (OpenAI Realtime) with **attention labs SAA** wired on top, built for **LiveKit Agents 1.5.x**.

## Why this one matters

A `RealtimeModel` has no swappable VAD slot, it handles its own turn-taking end to end. So stock LiveKit has **no place to hook an addressee gate**: the model hears every voice in the room and will answer side conversations, background TV, and the kids. SAA is the way to give a realtime model selective attention.

`session.input.set_audio_enabled(False)` detaches the input stream *upstream of* `RealtimeModel.push_audio`, so the model literally never receives the gated audio. `session.interrupt()` maps to the realtime provider's own cancel (`_rt_session.interrupt()`). Both are verified to work for realtime sessions.

## The integration

A single file ([`agent.py`](./agent.py)):

- `start_attention_session(...)`, summon the hidden SAA agent
- `@engine.on_prediction` → `session.input.set_audio_enabled(p.aligned_class == 2)`
- `@engine.on_interrupt` → `session.interrupt()`
- `@engine.on_interjection` → `session.generate_reply(...)`
- `@session.on("agent_state_changed")` → `responding_start`/`responding_stop` (so interrupt/interjection fire correctly)

## Quickstart

```bash
cd examples/livekit/voice_agent_realtime
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate

pip install -r requirements.txt
pip install -e ../../../packages/saa-livekit-client   # local dev against this repo

python agent.py dev
```

Connect a frontend (the [`web`](../web) sample, or the [LiveKit Agents Playground](https://agents-playground.livekit.io)) and talk.

## Gemini Live instead of OpenAI

One-line swap, install `livekit-plugins-google` and change:

```python
from livekit.plugins import google
...
session = AgentSession(llm=google.realtime.RealtimeModel(), vad=ctx.proc.userdata["vad"])
```

## Cost note

SAA is billed per session-minute; the realtime model is billed by the provider (OpenAI/Google).
