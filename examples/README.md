<p align="center">
  <a href="../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# Examples

Runnable integrations of SAA into voice-agent stacks. Each consumes a public package — no example depends on a private model artifact.

## LiveKit Agents

SAA joins your LiveKit room as a hidden participant (the hosted bridge) and gates the session through the `"saa"` data topic. Three samples in [`livekit/`](./livekit):

| Sample | What it shows | Run |
|---|---|---|
| [`livekit/voice_agent_cascaded/`](./livekit/voice_agent_cascaded) | SAA gating a cascaded pipeline (Silero VAD → Deepgram STT → OpenAI LLM → Cartesia TTS), with barge-in and proactive interjection. | `python src/agent.py dev` |
| [`livekit/voice_agent_realtime/`](./livekit/voice_agent_realtime) | SAA gating an OpenAI Realtime speech-to-speech agent — the case stock LiveKit can't gate (no VAD slot). | `python agent.py dev` |
| [`livekit/web/`](./livekit/web) | A vanilla HTML + `livekit-client` browser client rendering SAA's prediction overlay, plus a dev FastAPI token server. | `uvicorn token_server:app` |

All target **LiveKit Agents 1.5.x** (`AgentServer` + `@server.rtc_session()`). See [`livekit/README.md`](./livekit/README.md) for the shared environment and the five lines that integrate SAA.

## Pipecat (on Daily)

The Pipecat sibling. SAA joins your Daily room as a hidden participant and publishes events on Daily's app-message channel under the `"saa"` topic. Your Pipecat pipeline consumes them through `AttentionEngine`, which hooks `DailyTransport.event_handler("on_app_message")`. Two samples in [`pipecat/`](./pipecat):

| Sample | What it shows | Run |
|---|---|---|
| [`pipecat/voice_agent_cascaded/`](./pipecat/voice_agent_cascaded) | SAA gating a cascaded Pipecat pipeline (Silero VAD → Deepgram STT → OpenAI LLM → Cartesia TTS) on Daily, with barge-in and proactive interjection. | `python src/agent.py` |
| [`pipecat/web/`](./pipecat/web) | A vanilla HTML + `@daily-co/daily-js` browser client rendering SAA's prediction overlay, plus a dev FastAPI token server that creates an ephemeral Daily room. | `uvicorn token_server:app` |

Targets **pipecat-ai >= 1.0.0** (the `pipecat.transports.daily.transport` canonical import path) and **daily-python >= 0.19.0**. See [`pipecat/README.md`](./pipecat/README.md) for the shared environment.

## Roadmap

Packaged drop-in adapters for the stacks below are planned. They depend on an external-frame ingestion API landing in `attenlabs-saa` (so SAA can consume audio a framework already captured); until then these stacks are reachable by wiring the streaming SDK yourself.

| Stack | Shape |
|---|---|
| Twilio Media Streams | μ-law 8 kHz → PCM16 telephony bridge |
| OpenAI Realtime | browser/edge gating of a realtime model |
| ElevenLabs Conversational AI | WebRTC gating |
| Proactive-agent overlays | per-stack `mark_responding` lifecycle recipes |

## Conventions

- `ATTENLABS_TOKEN` is the streaming-SDK auth token; `SAA_API_KEY` is the hosted-bridge key (shared between LiveKit and Pipecat). Get one at [attentionlabs.ai](https://attentionlabs.ai).
- LiveKit samples additionally need `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, plus per-sample provider keys. See each sample's `.env.example`.
- Pipecat samples need `DAILY_API_KEY` (Daily REST, customer-owned — mints the hidden-bot meeting token locally; never seen by our broker), plus a `DAILY_ROOM_URL` + bot meeting token for the cascaded sample, plus per-sample provider keys.

## See also

- [`packages/saa-js/README.md`](../packages/saa-js/README.md), [`packages/saa-py/README.md`](../packages/saa-py/README.md) — streaming SDK reference.
- [`packages/saa-livekit-client/README.md`](../packages/saa-livekit-client/README.md) — LiveKit hosted bridge.
- [`packages/saa-pipecat-client/README.md`](../packages/saa-pipecat-client/README.md) — Pipecat-on-Daily hosted bridge.

---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
