<p align="center">
  <a href="../README.md">
    <img alt="SAA: Selective Attention" src="../assets/saa-hero.png" width="326">
  </a>
</p>

# Examples

Runnable SAA examples. The two **streaming-SDK demos** below show the SDK driving its own capture loop end to end; the rest integrate SAA into third-party voice-agent stacks (LiveKit, Pipecat, ElevenLabs, Twilio). Each consumes a public package, no example depends on a private model artifact.

## Get the code

```bash
git clone https://github.com/attenlabs/saa-sdk.git
cd saa-sdk
```

## Streaming SDK (start here)

The SDK captures mic + webcam itself, streams to SAA, and routes detected turns to an LLM (OpenAI Realtime shown). No agent framework in the loop, the shortest path from a token to live predictions. One demo per language:

| Sample | What it shows | Run |
|---|---|---|
| [`python/`](./python) | `attenlabs-saa` in a terminal: live ConvoStatus predictions, VAD, and conversation state in an ASCII status panel; turns routed to OpenAI Realtime. | `python main.py` |
| [`web/`](./web) | `@attenlabs/saa-js` in the browser: an orb UI + guided flow, turns routed to OpenAI Realtime. | `npx serve` |

Both need only a `SAA_API_KEY` (the Python demo also accepts `--token`; the web demo takes the token in its UI or via `?token=`). An OpenAI key is optional, omit it to just watch predictions.

## LiveKit Agents

SAA joins your LiveKit room and gates the session through the `"saa"` data topic. Two samples in [`livekit/`](./livekit):

| Sample | What it shows | Run |
|---|---|---|
| [`livekit/voice_agent_realtime/`](./livekit/voice_agent_realtime) | SAA gating an OpenAI Realtime speech-to-speech agent, with barge-in and proactive interjection. | `python agent.py dev` |
| [`livekit/web/`](./livekit/web) | A vanilla HTML + `livekit-client` browser client rendering SAA's prediction overlay, plus a dev FastAPI token server. | `uvicorn token_server:app` |

All target **LiveKit Agents 1.5.x** (`AgentServer` + `@server.rtc_session()`). See [`livekit/README.md`](./livekit/README.md) for the shared environment and the integration code.

## Pipecat (on Daily)

The Pipecat sibling. SAA joins your Daily room and publishes events on Daily's app-message channel under the `"saa"` topic. Your Pipecat pipeline consumes them through `AttentionEngine`, which hooks `DailyTransport.event_handler("on_app_message")`. One sample in [`pipecat/`](./pipecat):

| Sample | What it shows | Run |
|---|---|---|
| [`pipecat/web/`](./pipecat/web) | A vanilla HTML + `@daily-co/daily-js` browser client rendering SAA's prediction overlay, plus a dev FastAPI token server that creates an ephemeral Daily room. | `uvicorn token_server:app` |

Targets **pipecat-ai >= 1.0.0** (the `pipecat.transports.daily.transport` canonical import path) and **daily-python >= 0.19.0**. See [`pipecat/README.md`](./pipecat/README.md) for the shared environment.

## ElevenLabs Conversational AI

ElevenLabs runs its agent inside its own sealed WebRTC room, so this sample uses the **streaming SDK's `feed_audio` ingestion**: it taps ElevenLabs' Python `AudioInterface` (the clean PCM seam), feeds the user mic to SAA, and gates the agent on the events that come back.

| Sample | What it shows | Run |
|---|---|---|
| [`elevenlabs/voice_agent/`](./elevenlabs/voice_agent) | SAA gating an ElevenLabs Conversational AI agent so only device-directed speech reaches the model, via `attenlabs-saa`'s `feed_audio`. | `python agent.py` |

Needs **attenlabs-saa >= 0.6.0** and **elevenlabs >= 2.45**.

## Vapi WebSocket transport

Vapi exposes a raw PCM16 WebSocket seam, so this sample uses the **streaming SDK's `feed_audio` ingestion**: it captures the local mic with `sounddevice`, feeds every frame to SAA, and streams gated audio to Vapi's WebSocket transport.

| Sample | What it shows | Run |
|---|---|---|
| [`vapi/voice_agent/`](./vapi/voice_agent) | SAA gating a Vapi assistant over the WebSocket transport so only device-directed speech reaches the model, via `attenlabs-saa`'s `feed_audio`. | `python agent.py` |

Needs **attenlabs-saa >= 0.6.0**, **sounddevice**, and **websockets**. See [`vapi/README.md`](./vapi/README.md) for setup.

## Twilio Media Streams

Inbound or outbound PSTN phone calls, gated by SAA before any audio reaches STT or LLM. The adapter in [`twilio/media_streams/server.py`](./twilio/media_streams/server.py) transcodes μ-law 8 kHz Twilio frames to PCM16 16 kHz and feeds them to SAA via `feed_audio`. Only device-directed caller speech reaches the bridge; side talk and the agent's own TTS echo are filtered out. Three reference bridges are included: `LoggingBridge` (no keys, good for smoke-testing), `OpenAIRealtimeBridge`, and `DeepgramOpenAIElevenLabsBridge`.

| Sample | What it shows | Run |
|---|---|---|
| [`twilio/media_streams/`](./twilio/media_streams) | SAA-gated Twilio Media Streams adapter with μ-law ↔ PCM16 codec, paced outbound TTS sender, barge-in, and three reference bridges. | `python -m uvicorn server:app --port 8765` |

Needs **attenlabs-saa >= 0.6.1**, **fastapi**, **uvicorn**, **numpy**, and **twilio**. See [`twilio/README.md`](./twilio/README.md) for the full walk-through and limitations.

## Roadmap

`attenlabs-saa` ships `feed_audio` (external-frame ingestion), so any stack that already captures audio can be gated by feeding SAA.

| Stack | Shape |
|---|---|
| Proactive-agent overlays | per-stack `mark_responding` lifecycle recipes |

## Recommended usage

Try three send thresholds and keep the one that performs best for your use case:

- With video (webcam) enabled: `0.6`, `0.77`, `0.88`
- Audio-only: `0.5`, `0.7`, `0.8`

The send threshold is the confidence required to treat speech as device-directed. Raise it for fewer false triggers, lower it to catch quieter or borderline speech.

## Conventions

- `SAA_API_KEY` is your attention labs API key, the same credential for the streaming SDK and the LiveKit and Pipecat samples. Get one at [attentionlabs.ai](https://attentionlabs.ai).
- LiveKit samples additionally need `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, plus per-sample provider keys. See each sample's `.env.example`.
- Pipecat samples need `DAILY_API_KEY` (Daily REST key; mints the bot meeting token locally), plus per-sample provider keys.

## See also

- [`packages/saa-js/README.md`](../packages/saa-js/README.md), [`packages/saa-py/README.md`](../packages/saa-py/README.md): the streaming SDK reference.
- [`packages/saa-livekit-client/README.md`](../packages/saa-livekit-client/README.md): the LiveKit client.
- [`packages/saa-pipecat-client/README.md`](../packages/saa-pipecat-client/README.md): the Pipecat-on-Daily client.

---

<p align="center">
  <sub>An attention labs project. © 2026 Socero Inc.</sub>
</p>
