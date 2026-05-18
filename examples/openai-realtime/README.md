<p align="center">
  <a href="../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# openai-realtime

SAA is a pre-STT gate for an OpenAI Realtime session running in a
browser. SAA classifies each utterance before audio reaches the model;
only device-directed speech is forwarded to OpenAI Realtime.

| | |
|---|---|
| Run             | `make dev` (then open <http://localhost:8080>) |
| Demonstrates    | A browser SAA × OpenAI Realtime app: client-side gating, ephemeral-token relay, sample-rate matching, barge-in, and tool calling. |
| Expect          | Side conversations, background media, and the assistant's own playback are filtered before they reach the model. Speak directly to the laptop and the assistant replies. |
| Known gotcha    | Use ephemeral mode (`/session`); browser-direct API key is dev-only and exposes the key. |

## Run

```bash
cp .env.example .env       # ATTENLABS_TOKEN + OPENAI_API_KEY
make install
make dev                   # uvicorn server:app --reload --port 8080
```

Open <http://localhost:8080>, paste your SAA token, click **Start**.

## Env vars

| Var | Required | Default |
|---|---|---|
| `ATTENLABS_TOKEN` | yes | |
| `OPENAI_API_KEY` | yes | |
| `OPENAI_REALTIME_MODEL` | no | `gpt-realtime` |
| `OPENAI_VOICE` | no | `alloy` |
| `SAA_THRESHOLD` | no | `0.7` |
| `PORT` | no | `8080` |

## Token safety

The SAA token is pasted into the browser at runtime; the OpenAI key
stays server-side and is exchanged for a short-lived `client_secret` via
`/session`. The browser-direct mode is opt-in and clearly labelled
*dev only*. For production, mint short-lived rotating SAA tokens too
(see [`SECURITY.md`](../../SECURITY.md#token-rotation)).

## See also

- [`packages/saa-js/README.md`](../../packages/saa-js/README.md): SDK reference.
- [`examples/proactive-agent/openai-realtime/`](../proactive-agent/openai-realtime/): the proactive overlay built on this adapter.
- [`examples/README.md`](../README.md): adapter index.


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
