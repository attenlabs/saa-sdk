<p align="center">
  <img alt="SAA: Selective Auditory Attention" src="./assets/saa-hero.png" width="408">
</p>

<h3 align="center">Tells your voice agent which speech is actually for it.</h3>

<p align="center">One decision per utterance: only addressee speech reaches your STT, LLM, and TTS. No wake word.</p>

<p align="center">
  <a href="https://www.npmjs.com/package/@attenlabs/saa-js"><img alt="npm" src="https://img.shields.io/npm/v/@attenlabs/saa-js?label=npm&color=D9FF00&labelColor=060d0f&style=for-the-badge"></a>
  <a href="https://pypi.org/project/attenlabs-saa/"><img alt="PyPI" src="https://img.shields.io/pypi/v/attenlabs-saa?label=pypi&color=D9FF00&labelColor=060d0f&style=for-the-badge"></a>
  <a href="./LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-D9FF00?labelColor=060d0f&style=for-the-badge"></a>
</p>

<p align="center">Drop-in for the voice-agent stack you already use:</p>
<p align="center">
  <a href="./examples/pipecat/"><img alt="Pipecat" src="./assets/brands/pipecat.svg" height="28"></a>
  &nbsp;&nbsp;
  <a href="./examples/livekit/"><img alt="LiveKit" src="./assets/brands/livekit.svg" height="28"></a>
  &nbsp;&nbsp;
  <a href="./examples/elevenlabs/"><img alt="ElevenLabs Conversational AI" src="./assets/brands/elevenlabs.svg" height="28"></a>
  &nbsp;&nbsp;
  <a href="./examples/twilio/"><img alt="Twilio Media Streams" src="./assets/brands/twilio.svg" height="28"></a>
</p>

## What is SAA?

A voice agent's microphone hears every voice in the room: yours, a coworker's, the kids, a podcast playing on the laptop, the agent's own TTS bleeding back through the speakers. Most pipelines respond to any of it, paying STT for every transcribed second and triggering the LLM on speech that was never directed at the device.

<table>
  <thead>
    <tr>
      <th align="center" width="33%">Single device &middot; robot</th>
      <th align="center" width="33%">Single device &middot; laptop</th>
      <th align="center" width="34%">Multi-device &middot; two robots</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td align="center"><img alt="Pollen Robotics Reachy robot listening; SAA fires only when the speaker addresses it." src="./assets/reachy-demo-loop.gif" width="100%"></td>
      <td align="center"><img alt="Live laptop session. SAA gates speech in a real browser tab; the pill flips green only when the user is addressing the laptop, ambient room speech stays gray." src="./assets/use-cases/laptop.gif" width="100%"></td>
      <td align="center"><img alt="Two Pollen Robotics Reachy robots in the same room, hearing the same audio. Only the addressed robot acts; the other stays still." src="./assets/use-cases/reachy.gif" width="100%"></td>
    </tr>
    <tr>
      <td align="center"><sub>Only addressed speech wakes the robot.</sub></td>
      <td align="center"><sub>Pill flips green only when the user addresses the screen.</sub></td>
      <td align="center"><sub>Same room, same audio. Only the addressed robot acts.</sub></td>
    </tr>
  </tbody>
</table>

**SAA** (Selective Auditory Attention) is a hosted classifier that runs **before STT** and decides, per utterance, whether the speech was directed at the device. Side talk, background media, and the agent's own playback are filtered out, so your STT / LLM / TTS only see audio meant for the agent.

- **No wake word.** SAA decides per-utterance from the audio (and optionally low-rate video) stream.
- **Hosted.** A real-time WebSocket to attention labs' cloud; the model, weights, and inference run server-side, and these Apache-2.0 client SDKs are thin: they capture, encode, and stream audio (and optional low-rate video). Because it gates before STT, only addressed speech reaches the STT, LLM, and TTS you already run, so your downstream services and logs see less audio, not more. On-device deployment is a separate enterprise licence.

The architecture and evaluation are described in the [technical report](https://arxiv.org/abs/2604.08412).

## Ways to integrate

| Shape | Package | Use it when |
|---|---|---|
| **Streaming SDK** | [`@attenlabs/saa-js`](./packages/saa-js), [`attenlabs-saa`](./packages/saa-py) | your app captures the audio/video itself and you want typed attention events to gate your own pipeline. Good for web agents, mall kiosks, drive-through agents, and robots. |
| **LiveKit** | [`saa-livekit-client`](./packages/saa-livekit-client) | you run a [LiveKit Agents](https://docs.livekit.io/agents/) voice agent. SAA joins your room and gates the session. |
| **Pipecat (Daily)** | [`saa-pipecat-client`](./packages/saa-pipecat-client) | you run a [Pipecat](https://github.com/pipecat-ai/pipecat) voice agent on Daily. SAA joins your Daily room and gates the pipeline through the `"saa"` app-message topic. |
| **ElevenLabs** | [`attenlabs-saa`](./packages/saa-py) | you run an [ElevenLabs Conversational AI](./examples/elevenlabs) agent. SAA gates it via the streaming SDK's `feed_audio` (its room is sealed, so SAA can't join it directly). |
| **Twilio** | [`attenlabs-saa`](./packages/saa-py) | you run a [Twilio Media Streams](https://www.twilio.com/docs/voice/media-streams) telephony agent. SAA gates inbound/outbound call audio (μ-law 8 kHz resampled to PCM16) via the streaming SDK's `feed_audio`. |

## Install

```bash
npm install @attenlabs/saa-js     # JavaScript / browser
pip install attenlabs-saa          # Python (streaming SDK)
pip install saa-livekit-client     # Python (LiveKit)
pip install saa-pipecat-client     # Python (Pipecat on Daily)
```

Get an API key at [attentionlabs.ai/dashboard](https://attentionlabs.ai/dashboard).

## Streaming SDK

You capture the media; SAA emits typed events. The key event is `turnReady` / `turn_ready`, one device-directed utterance, captured and ready to forward to your STT or LLM.

```js
import { AttentionClient } from "@attenlabs/saa-js";

const client = new AttentionClient({ token: process.env.SAA_API_KEY });

// fires once per device-directed turn; turn.audioBase64 is PCM16 @ 16 kHz
client.on("turnReady", (turn) => yourSTT.send(turn.audioBase64));

await client.start({ videoElement: document.querySelector("video") });
```

```python
import os
from saa import AttentionClient

client = AttentionClient(token=os.environ["SAA_API_KEY"])

@client.on_turn_ready
def _(turn):
    # turn.audio_base64, PCM16 @ 16 kHz mono; turn.audio_pcm16, np.int16 array
    your_stt.send(turn.audio_base64)

client.start()
```

For audio-only deployments, omit `videoElement` (browser) or pass `enable_video=False` (Python).

Both SDKs also emit `prediction`, `vad`, `state`, `interrupt`, `config`, and `stats` events, and expose `mute()` / `unmute()`, `setThreshold()` / `set_threshold()`, and `markResponding()` / `mark_responding()`. See [`packages/saa-js`](./packages/saa-js) and [`packages/saa-py`](./packages/saa-py).

Runnable end-to-end demos are in [`examples/web/`](./examples/web) (browser) and [`examples/python/`](./examples/python) (terminal).

## LiveKit

For [LiveKit Agents](https://docs.livekit.io/agents/), `saa-livekit-client` brings SAA into your room to run the classifier and publish events on the `"saa"` data topic. Your agent consumes them through `AttentionEngine` and gates the session.

```python
from saa_livekit_client import AttentionEngine, attention_agent_token, start_attention_session

saa = await start_attention_session(
    api_key=SAA_API_KEY, livekit_url=LIVEKIT_URL,
    agent_token=attention_agent_token(api_key=LK_KEY, api_secret=LK_SECRET, room_name=ctx.room.name),
    room_name=ctx.room.name, participant_identity=user.identity,
)
engine = AttentionEngine(ctx.room, agent_identity=saa.agent_identity)

@engine.on_prediction
def _(p):
    session.input.set_audio_enabled(p.aligned_class == 2)   # the gate

await engine.start()
```

Two runnable samples, an OpenAI Realtime agent and a vanilla-JS web client, are in [`examples/livekit/`](./examples/livekit).

## Pipecat on Daily

For [Pipecat](https://github.com/pipecat-ai/pipecat) voice agents running on Daily, `saa-pipecat-client` brings SAA into your Daily room and publishes events on Daily's app-message channel under the `"saa"` topic. Your bot consumes them through `AttentionEngine` (which subscribes via your `DailyTransport`) and gates the pipeline.

```python
from saa_pipecat_client import AttentionEngine, attention_agent_token, start_attention_session

saa = await start_attention_session(
    api_key=SAA_API_KEY, room_url=ROOM_URL,
    agent_token=attention_agent_token(daily_api_key=DAILY_API_KEY, room_name=room_name),
    participant_identity=human_identity,
)
engine = AttentionEngine(transport, agent_identity=saa.agent_identity)
engine.bind_task(task)

@engine.on_prediction
def _(p):
    addressee_gate.suppressed = (p.aligned_class == 1 and p.confidence > 0.7)

await engine.start()
```

A runnable web-client sample is in [`examples/pipecat/`](./examples/pipecat).

## ElevenLabs

[ElevenLabs Conversational AI](https://elevenlabs.io/docs/eleven-agents/overview) runs its agent in a sealed WebRTC room, so SAA can't join it directly. Instead the streaming SDK runs in feed mode: you hand it the agent's microphone audio through `feed_audio` and gate the agent on SAA's `prediction` events.

```python
from saa import AttentionClient

# feed mode: the SDK captures nothing itself; you supply the audio
saa = AttentionClient(token=SAA_API_KEY, enable_audio=False, enable_video=False)

@saa.on_prediction
def _(p):
    mic_to_agent.enabled = (p.aligned_class == 2)   # 2 = addressed to the device

saa.start()
# in ElevenLabs' AudioInterface input callback:
saa.feed_audio(mic_pcm16)
```

A runnable sample is in [`examples/elevenlabs/`](./examples/elevenlabs).

## Twilio

For [Twilio Media Streams](https://www.twilio.com/docs/voice/media-streams) telephony agents, the streaming SDK runs in feed mode over the call audio. The adapter transcodes Twilio's μ-law 8 kHz frames to PCM16 16 kHz, feeds them to SAA, and forwards only device-directed turns to your bridge, so side talk, hold music, and the agent's own TTS echo are gated out.

```python
from saa import AttentionClient

saa = AttentionClient(token=SAA_API_KEY, enable_audio=False, enable_video=False)

@saa.on_turn_ready
def _(turn):
    bridge.on_speech(turn.audio_base64)   # only device-directed call audio continues

saa.start()
# in the Twilio media handler, after decoding μ-law -> PCM16:
saa.feed_audio(pcm16_frames)
```

A runnable Media Streams bridge (codec, paced outbound, automatic `mark_responding`) is in [`examples/twilio/`](./examples/twilio).

## Proactive agents (speak first)

The streaming SDKs expose `markResponding(true)` / `mark_responding(True)` so the agent can assert when *it* is the one speaking, suppressing the gate during its own TTS and resuming once the tail clears. The LiveKit and Pipecat bridges expose the same lifecycle via `engine.responding_start()` / `responding_stop()`, identical surface.


## How it composes

SAA is the model-agnostic addressee decision between your VAD and STT. It answers a different question than VAD (is anyone speaking), speaker diarization (which voice it is), turn detection (have they finished), or a wake word (did they say the phrase), so it composes with those layers and can replace the wake word outright.

<p align="center">
  <img alt="Where SAA sits in your voice stack: noise suppression and VAD upstream, SAA addressee gate, then STT → LLM → TTS downstream" src="./assets/diagrams/where-saa-sits-dark.svg" width="820">
</p>

## On-device deployment

These Apache-2.0 client SDKs stream to the SAA cloud. For deployments where audio must stay on the device (telephony, embedded systems, wearables, robotics, kiosks), request on-device and embedded access at [attentionlabs.ai](https://attentionlabs.ai/#contact).

## Documentation

- [`packages/saa-js/README.md`](./packages/saa-js/README.md), [`packages/saa-py/README.md`](./packages/saa-py/README.md), streaming SDK reference.
- [`packages/saa-livekit-client/README.md`](./packages/saa-livekit-client/README.md): the LiveKit client.
- [`packages/saa-pipecat-client/README.md`](./packages/saa-pipecat-client/README.md): the Pipecat-on-Daily client.
- [`examples/README.md`](./examples/README.md), runnable examples.
- [`examples/twilio/README.md`](./examples/twilio/README.md): the Twilio Media Streams bridge.

## License

Apache-2.0 across the repo, each package and the examples ship under it (see each subtree's `LICENSE`). The hosted cloud service is governed by the attention labs Terms of Service.

[`SECURITY.md`](./SECURITY.md) · [`CONTRIBUTING.md`](./CONTRIBUTING.md) · [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md) · [`CHANGELOG.md`](./CHANGELOG.md) · [`NOTICE`](./NOTICE) · [`CITATION.cff`](./CITATION.cff)

---

<p align="center">
  <sub>An attention labs project. © 2026 Socero Inc.</sub>
</p>
