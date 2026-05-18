<p align="center">
  <a href="../../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# proactive-agent / pipecat

**Proactive meeting / note-taker / follow-up agent on Pipecat.** Thin overlay on [`examples/pipecat/`](../../pipecat/) that adds a mid-call `POST /trigger` so a back-end (CRM, action-item detector, scheduler) can make the agent speak first at any time.

| | |
|---|---|
| Run             | `make dev` (starts `pipecat-runner` with the proactive bot + HTTP `/trigger` sidecar), then `INSTRUCTIONS="…" make trigger` |
| Demonstrates    | Mid-call proactive turn injected as `LLMRunFrame` into the live `PipelineTask`; the parent's `SAAGate(suppress_during_bot_speech=True)` handles `mark_responding` automatically across TTS playback; STT only sees device-directed audio. |
| Expect          | The bot's TTS opens with the action-item line; side conversations during the meeting do NOT reach Deepgram; the user's actual reply does. |
| Built on        | [`examples/pipecat/`](../../pipecat/): `SAAGate` FrameProcessor, transport adapters (Daily / SmallWebRTC / Twilio / Telnyx / Plivo), Deepgram + OpenAI + Cartesia plug-ins. Nothing duplicated. |

## Trigger → Without SAA → With SAA

| Trigger | Without SAA | With SAA |
|---|---|---|
| `POST /trigger` from an action-item detector, CRM, or meeting scheduler. | TTS opens with the proactive line; Deepgram then transcribes every voice in the room: side conversations, the bot's own echo on a poor mic, anyone walking by. | TTS opens with the proactive line; the parent gate's `mark_responding` wiring suppresses bot bleed; only the addressee's reply reaches the STT plug-in. |

## What this overlay adds

- [`proactive_bot.py`](./proactive_bot.py): the parent bot plus an HTTP `/trigger` sidecar that pushes a developer-role message into the LLM context and queues an `LLMRunFrame`.
- [`demo_script.json`](./demo_script.json): operator-edited campaign script.

The parent's `on_client_connected` already injects an opening `LLMRunFrame`; this overlay adds a second trigger surface for mid-call turns.

## Run it

```bash
cd examples/proactive-agent/pipecat
make install
cp .env.example .env && $EDITOR .env
make dev
```

Connect via your Pipecat transport (`daily`, `smallwebrtc`, `twilio`, `telnyx`, `plivo`). The opening turn fires on client-connect; fire a mid-call proactive turn:

```bash
make trigger
INSTRUCTIONS="That was a long pause. Everything OK?" make trigger
curl -X POST http://localhost:8765/trigger \
  -H 'content-type: application/json' \
  -d '{"instructions": "Reminder: standup in 5 minutes."}'
```

## Common failures

| Symptom | Fix |
|---|---|
| `pipecat-runner` exits with `Missing required env vars` | One of `ATTENLABS_TOKEN`, `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, `CARTESIA_API_KEY` is unset. |
| `make trigger` returns `connection refused` | The sidecar didn't bind. Check `make dev` logs for `HTTP sidecar listening on :8765`; if port collision, set `PROACTIVE_HTTP_PORT=8766`. |
| Trigger fires but the bot never speaks | Pipeline isn't running yet. Connect a transport client first. |
| Deepgram bills on background audio | Verify `audio_in_sample_rate=SAA_SAMPLE_RATE` in the transport params (parent's `bot.py:160-167`). |

## See also

- [`examples/pipecat/`](../../pipecat/): the underlying reactive adapter.
- [`packages/saa-proactive-py/README.md`](../../../packages/saa-proactive-py/README.md): the lifecycle wrapper package.
- [`packages/saa-py/README.md`](../../../packages/saa-py/README.md): SDK reference.


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
