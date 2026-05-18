<p align="center">
  <a href="../../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# proactive-agent / twilio

**The agent speaks first on every outbound call.** Thin overlay on [`examples/twilio/`](../../twilio/): an AI-SDR bridge that emits the opening turn the moment the call connects, plus an HTTP webhook (`POST /place-proactive-call`) so a CRM, scheduler, or notification trigger can place the call from anywhere.

| | |
|---|---|
| Run             | `make dev`, then `TO=+15551112222 make place-call` |
| Demonstrates    | Agent-initiated opening turn over PSTN + SAA gating the callee's reply against coworkers, kids, hold music, third-party conversation. `mark_responding(True)` auto-fires on the first outbound byte so the agent's own TTS bleed doesn't re-fire the gate. |
| Expect          | Opening line plays; background coworker speech is silently dropped; the callee's actual reply reaches the LLM. |
| Built on        | [`examples/twilio/`](../../twilio/): server, μ-law codec, signature validation, paced playback, barge-in. Nothing duplicated. |

## Trigger → Without SAA → With SAA

| Trigger | Without SAA | With SAA |
|---|---|---|
| `POST /place-proactive-call` fires from a CRM, scheduler, or notification rule. | Outbound TTS plays; the gate then hears the callee's hello, coworker chatter, hold music, IVR menus, so every voiced segment becomes a candidate user turn. | Outbound TTS plays; `mark_responding(True)` auto-fires on the first outbound byte; only the callee's device-directed reply unmutes the LLM. |

## What this overlay adds

- `AISDRBridge` (in [`ai_sdr_bridge.py`](./ai_sdr_bridge.py)): a subclass of the parent's `OpenAIRealtimeBridge` that emits a single `response.create` in `open()` so the agent speaks the campaign opening line the instant the call connects.
- `main.py`: a 60-line overlay that imports the Twilio adapter's FastAPI app, registers `AISDRBridge` as the per-call bridge factory, and adds `POST /place-proactive-call` for webhook-triggered campaigns.
- `place_proactive_call.py`: a CLI that places a single outbound call via the existing `examples/twilio/outbound.py` dialer.
- `demo_script.json`: the campaign script (opening line, system prompt, voice, model). Operators edit this without redeploying.

The full SAA control surface is inherited from the parent adapter: `mark_responding` (auto-fired on outbound bytes), `mute` / `unmute`, `set_threshold`, barge-in via `on_user_speech_started`.

## Run it

```bash
cd examples/proactive-agent/twilio
make install
cp .env.example .env && $EDITOR .env
#   ATTENLABS_TOKEN, OPENAI_API_KEY, TWILIO_ACCOUNT_SID,
#   TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, PUBLIC_HOSTNAME
$EDITOR demo_script.json     # opening line + system prompt
make dev                     # uvicorn main:app --reload
make tunnel                  # optional: ngrok so Twilio can reach /voice
```

Place a call:

```bash
TO=+15551112222 make place-call
curl -X POST http://localhost:8000/place-proactive-call \
  -H 'content-type: application/json' \
  -d '{"to": "+15551112222"}'
```

## Common failures

| Symptom | Fix |
|---|---|
| `make place-call` returns `401 unauthorized` | `TWILIO_AUTH_TOKEN` missing or stale. Copy from <https://console.twilio.com/>. |
| `WebSocket signature validation failed` | `PUBLIC_HOST` does not match the URL Twilio is dialling. Verify it points at your public tunnel. |
| Outbound call rings but disconnects in 2-3 s | `FROM` number not verified or out of credit. Trial accounts also need the `TO` number verified. |
| Opening line plays but SAA never fires `speech_ready` on the reply | Threshold too strict for noisy carriers. Drop `SAA_THRESHOLD` from `0.82` to `0.70` and restart. |
| `OpenAI: invalid_api_key` after the call connects | The Realtime API needs a key with Realtime preview access. |

## See also

- [`examples/twilio/`](../../twilio/): the underlying Twilio adapter.
- [`packages/saa-proactive-py/README.md`](../../../packages/saa-proactive-py/README.md): the lifecycle wrapper package.
- [`packages/saa-py/README.md`](../../../packages/saa-py/README.md): SDK reference.


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
