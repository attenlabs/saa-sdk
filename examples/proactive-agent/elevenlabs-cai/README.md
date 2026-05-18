<p align="center">
  <a href="../../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# proactive-agent / elevenlabs-cai

**Proactive browser voice agent on ElevenLabs Conversational AI.** Thin overlay on [`examples/elevenlabs-cai/`](../../elevenlabs-cai/) that adds a `POST /proactive-trigger` so a back-end webhook can make the agent speak first.

| | |
|---|---|
| Run             | `make dev`, then click **Trigger proactive turn** at `http://localhost:8000/proactive.html` or `INSTRUCTIONS="…" make trigger` |
| Demonstrates    | Browser-side `convo.sendUserMessage(...)` forces a proactive agent turn over WebRTC; `markResponding(true)` is asserted manually at trigger time and again via `onModeChange("speaking")`; SSE `POST /proactive-trigger → /proactive-events` fan-out lets back-end webhooks reach every connected browser. |
| Expect          | Opening line plays; phone-call audio, kids, podcast bleed all classify as human-directed and drop; only screen-directed replies reach the agent. |
| Built on        | [`examples/elevenlabs-cai/`](../../elevenlabs-cai/): server-minted CAI tokens (`xi-api-key` never reaches the browser), WebRTC session lifecycle, `onModeChange → markResponding`. Nothing duplicated. |

## Trigger → Without SAA → With SAA

| Trigger | Without SAA | With SAA |
|---|---|---|
| `POST /proactive-trigger` from a campaign job, drip-marketing scheduler, or in-app event. | The campaign opening plays; CAI then hears the phone next to the laptop, kids in the background, the podcast still playing. | The campaign opening plays; `markResponding(true)` asserts immediately; only screen-directed audio reaches the ElevenLabs agent. |

## What this overlay adds

- [`proactive_server.py`](./proactive_server.py) extends the parent's FastAPI `app` with `POST /proactive-trigger` and `GET /proactive-events` (Server-Sent Events).
- [`proactive.html`](./proactive.html) + [`proactive.js`](./proactive.js) are a browser overlay that uses `convo.sendUserMessage(...)` (a synthetic user turn) to force the agent to speak first. `markResponding(true)` is asserted both manually at trigger time and via the parent's `onModeChange` handler when the agent transitions to `speaking`.
- [`demo_script.json`](./demo_script.json) carries the campaign opening line.

## Run it

```bash
cd examples/proactive-agent/elevenlabs-cai
make install
cp .env.example .env && $EDITOR .env     # ELEVENLABS_API_KEY + ELEVENLABS_AGENT_ID + ATTENLABS_TOKEN
make dev
```

Open <http://localhost:8000/proactive.html>, click **Start session**, grant mic permission. Fire a proactive turn:

```bash
make trigger
INSTRUCTIONS="Special offer: 20% off if you upgrade today." make trigger
curl -X POST http://localhost:8000/proactive-trigger \
  -H 'content-type: application/json' \
  -d '{"instructions": "Reminder: your trial ends tomorrow."}'
```

## Common failures

| Symptom | Fix |
|---|---|
| Browser console: `WebSocket: 401` | `ELEVENLABS_API_KEY` invalid. Refresh from <https://elevenlabs.io/app/settings/api-keys>. |
| `Agent not found` | `ELEVENLABS_AGENT_ID` empty or pointing at a different workspace. Create an agent at <https://elevenlabs.io/app/conversational-ai>. |
| Trigger fires but no audio | Mic permission denied OR autoplay policy blocking WebRTC. Grant mic; click in the page once before triggering. |
| Trigger response is `503` | No browser is connected to the SSE stream. Open `/proactive.html` and click **Start session** first. |
| `sendUserMessage` does not exist on `convo` | `@elevenlabs/client` version too old. The overlay falls back to `sendContextualUpdate` automatically. |

## See also

- [`examples/elevenlabs-cai/`](../../elevenlabs-cai/): the underlying reactive adapter.
- [`packages/saa-proactive-js/README.md`](../../../packages/saa-proactive-js/README.md): the lifecycle wrapper package.
- [`packages/saa-js/README.md`](../../../packages/saa-js/README.md): SDK reference.


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
