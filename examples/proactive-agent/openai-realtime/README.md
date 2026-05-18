<p align="center">
  <a href="../../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# proactive-agent / openai-realtime

**Proactive in-app voice agent.** Thin overlay on [`examples/openai-realtime/`](../../openai-realtime/): the agent speaks first when a build breaks, a long-running task completes, or a back-end event fires. SAA gates the reply against Zoom calls, coworkers, and the agent's own TTS bleed.

| | |
|---|---|
| Run             | `make dev`, then click **Trigger proactive turn** in the browser or `INSTRUCTIONS="…" make trigger` |
| Demonstrates    | Browser-side `markResponding(true)` fired before `response.create`, server-side SSE fan-out from `POST /proactive-trigger` to every connected browser, OpenAI Realtime opening turn with no preceding user audio. |
| Expect          | Opening line plays in the browser; off-screen speech during the reply window classifies as human-directed and OpenAI does not respond; look at the screen and reply and the LLM continues. |
| Built on        | [`examples/openai-realtime/`](../../openai-realtime/): ephemeral `client_secret` mint, static file server, browser SDK wiring. Nothing duplicated. |

## Trigger → Without SAA → With SAA

| Trigger | Without SAA | With SAA |
|---|---|---|
| `POST /proactive-trigger` from a build-status webhook, long-running task, or notification rule. | Opening line plays; OpenAI Realtime then hears every microphone byte: coworker conversation, the agent's own TTS bleed, ambient noise. | Opening line plays; `markResponding(true)` fires before `response.create`; only device-directed `speechReady` frames reach the model. |

## What this overlay adds

- `proactive_server.py` extends the parent FastAPI `app` with `POST /proactive-trigger` (back-end webhooks call this) and `GET /proactive-events` (SSE stream the browser subscribes to).
- `proactive.html` + `proactive.js` assert `markResponding(true)` **before** sending `response.create` (the parent's reactive `main.js` only asserts it on `response.audio.delta`, which is too late for a proactive turn with zero preceding user audio).
- `demo_script.json` carries the opening line and system prompt so operators can edit without redeploying.

## The proactive lifecycle

1. Back-end webhook arrives at `POST /proactive-trigger`.
2. The relay broadcasts the trigger to every connected browser via `GET /proactive-events`.
3. The browser asserts `saa.markResponding(true)` immediately.
4. The browser sends `response.create` to OpenAI Realtime with the opening instructions.
5. OpenAI synthesises the opening line; the browser plays it back.
6. On `response.done`, the browser asserts `saa.markResponding(false)` after a one-tick wait for trailing audio chunks.
7. SAA classifies every subsequent 100 ms frame; only `speechReady` audio is forwarded to OpenAI as the user's reply.

## Run it

```bash
cd examples/proactive-agent/openai-realtime
make install
cp .env.example .env && $EDITOR .env     # ATTENLABS_TOKEN + OPENAI_API_KEY
make dev                                  # uvicorn proactive_server:app
```

Open <http://localhost:8000/proactive.html>, click **Start session**, grant mic. The agent is silent until a proactive event fires:

```bash
make trigger
INSTRUCTIONS="Build broke." make trigger
curl -X POST http://localhost:8000/proactive-trigger \
  -H 'content-type: application/json' \
  -d '{"instructions": "Tests are red. Want me to look?"}'
```

For coding-copilot-style confirm-before-destructive-action flows, set `SAA_THRESHOLD=0.82` in `.env` (stricter threshold) and use a destructive-confirmation prompt.

## Common failures

| Symptom | Fix |
|---|---|
| Browser console: `net::ERR_BLOCKED_BY_CLIENT` on esm.sh | Ad-blocker. Allow `esm.sh` or self-host the bundle. |
| `make trigger` returns `503 no subscribers connected` | No browser subscribed to the SSE stream. Open `/proactive.html` and click **Start session** first. |
| **Start session** button does nothing | `ATTENLABS_TOKEN` or `OPENAI_API_KEY` missing in `.env`. |
| Trigger fires but no audio plays | Mic permission denied or autoplay policy blocking WebRTC playback. Click in the page first. |
| Port 8000 already in use | `PORT=8080 make dev`. |

## See also

- [`examples/openai-realtime/`](../../openai-realtime/): the underlying reactive adapter.
- [`packages/saa-proactive-js/README.md`](../../../packages/saa-proactive-js/README.md): the lifecycle wrapper package.
- [`packages/saa-js/README.md`](../../../packages/saa-js/README.md): SDK reference.


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
