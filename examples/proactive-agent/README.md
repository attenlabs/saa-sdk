<p align="center">
  <a href="../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# proactive-agent

**Make any voice agent proactive.** The agent speaks first; the user's first utterance is a reply. SAA gates that reply through the agent's TTS bleed using the `mark_responding(True/False)` lifecycle.

Five variants, one per framework SAA's reactive examples cover. Each variant is a thin overlay on the parent adapter plus a documented HTTP trigger surface so a CRM, scheduler, calendar, or back-end webhook can make the agent speak first.

## Pick your framework

| Framework | When to use | Variant |
|---|---|---|
| **Twilio Media Streams** | AI-SDR / outbound voice, PSTN telephony. The agent always speaks first on every outbound call. | [`twilio/`](./twilio/) |
| **OpenAI Realtime** | In-app / laptop / browser voice agent. Coding co-pilot, customer-support dashboard. | [`openai-realtime/`](./openai-realtime/) |
| **Pipecat** | Meeting follow-up, post-call action items, cloud transports (Daily / SmallWebRTC / Twilio / Telnyx). | [`pipecat/`](./pipecat/) |
| **LiveKit Agents** | Room-based video conferencing. A `POST /trigger` sidecar dispatches the proactive turn via `session.generate_reply(instructions=...)`. | [`livekit/`](./livekit/) |
| **ElevenLabs CAI** | Browser WebRTC voice agent. `POST /proactive-trigger` synthesises a user turn via `convo.sendUserMessage` (with `sendContextualUpdate` fallback for older `@elevenlabs/client`). | [`elevenlabs-cai/`](./elevenlabs-cai/) |

## Shape every variant shares

1. **A campaign script** (`demo_script.json`, identical shape across variants) carrying the opening line and system prompt.
2. **A proactive overlay** that subclasses the parent adapter's bridge / agent / handler and emits the proactive turn in the right place for that framework's lifecycle.
3. **A documented HTTP trigger endpoint** (`POST /trigger` for Pipecat + LiveKit, `POST /proactive-trigger` for OpenAI Realtime + ElevenLabs CAI, `POST /place-proactive-call` for Twilio) that lets a CRM webhook, scheduler, or back-end fire the proactive turn from outside the agent process.
4. **`mark_responding(True/False)`** auto-fired by the parent adapter on the first outbound byte (Twilio, Pipecat, LiveKit) or asserted manually by the proactive overlay (OpenAI Realtime, ElevenLabs CAI).
5. **An offline shape test** (`test_smoke_shape.{py,mjs}`) that asserts the proactive lifecycle without any cloud account.

## Run any of them

Every variant ships the same install / test-shape / dev Makefile targets:

```bash
cd examples/proactive-agent/<variant>
make install        # pip install -r requirements.txt (and the parent variant's deps)
make test-shape     # offline; no cloud account needed
cp .env.example .env && $EDITOR .env
make dev            # run the agent
```

The trigger target and HTTP endpoint differ by variant:

| Variant | CLI target | HTTP endpoint | Default port |
|---|---|---|---|
| `pipecat`, `livekit` | `make trigger` | `POST /trigger` | 8765 |
| `openai-realtime`, `elevenlabs-cai` | `make trigger` | `POST /proactive-trigger` | 8000 |
| `twilio` | `TO=+15551112222 make place-call` | `POST /place-proactive-call` | 8000 |

For example, against the Pipecat overlay:

```bash
curl -X POST http://localhost:8765/trigger \
  -H 'content-type: application/json' \
  -d '{"instructions": "Your tests are red. Want me to look?"}'
```

## See also

- [`packages/saa-proactive-js/README.md`](../../packages/saa-proactive-js/README.md), [`packages/saa-proactive-py/README.md`](../../packages/saa-proactive-py/README.md): the lifecycle helper packages every overlay uses.
- [`packages/saa-gate/README.md`](../../packages/saa-gate/README.md): the cross-framework routing policy state machine.
- [`examples/README.md`](../README.md): the full adapter index.
- [`packages/saa-js/README.md`](../../packages/saa-js/README.md), [`packages/saa-py/README.md`](../../packages/saa-py/README.md): SDK reference.


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
