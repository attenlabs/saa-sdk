<p align="center">
  <a href="../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# elevenlabs-cai

A browser app that wires SAA in front of an ElevenLabs Conversational AI agent. The browser holds the SAA token; the ElevenLabs API key stays server-side and is exchanged for a short-lived WebRTC session token.

| | |
|---|---|
| Run             | `make dev` (then open <http://localhost:8080>) |
| Demonstrates    | A browser SAA × ElevenLabs CAI app: server-minted WebRTC tokens, `setMicMuted` driven by the SAA verdict, `sendContextualUpdate` from `speechReady` + face count, and three `clientTools` the agent can invoke. A `/twilio` relay carries the same surface for PSTN. |
| Expect          | The CAI agent speaks freely; SAA mutes the mic during background talk and unmutes it on directed speech. Three tools let the agent ask SAA back about the user's gaze, face count, or current threshold. |
| Known gotcha    | The `xi-api-key` MUST stay server-side. The browser never sees it; it gets only a WebRTC session token from `/session`. |

## Run

```bash
cp .env.example .env       # ATTENLABS_TOKEN + ELEVENLABS_API_KEY + AGENT_ID
make install
make dev                   # FastAPI relay + static browser at http://localhost:8080
```

For PSTN through Twilio: `make tunnel` (ngrok), point Twilio at `https://<host>/twilio`.

## Env vars

| Var | Required | Default |
|---|---|---|
| `ATTENLABS_TOKEN` | yes | |
| `ELEVENLABS_API_KEY` | yes | |
| `ELEVENLABS_AGENT_ID` | yes | |
| `ELEVENLABS_CONNECTION_TYPE` | no | `webrtc` |
| `SAA_THRESHOLD` | no | `0.7` |
| `SAA_GATE_MODE` | no | `mic` |
| `HOST`, `PORT` | no | `0.0.0.0`, `8080` |

## What's wired beyond the official SDK

- **`setMicMuted` driven by SAA**: directed-speech opens the mic, non-directed mutes it.
- **`sendContextualUpdate` from SAA events**: the agent gets a hint when a new device-directed turn starts.
- **Three `clientTools`**: the agent can call `saa_get_gaze`, `saa_get_face_count`, and `saa_get_threshold` to introspect the gate.
- **PSTN bridge**: `/twilio` carries the same wiring over Twilio Media Streams.

## Files

- [`server.py`](./server.py): FastAPI relay providing `/session` (mint), `/twilio` (PSTN), and static assets.
- [`main.js`](./main.js): browser client wiring SAA, EL/CAI WebRTC, and tool registration.
- [`index.html`](./index.html): minimal UI.

## Tests

```bash
make test-shape          # no-network shape check (CI uses this)
make test                # full pytest if installed
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Browser can't connect to EL/CAI | The `xi-api-key` MUST be on the server; the browser only receives the short-lived WebRTC token from `/session`. |
| SAA never mutes the mic | Open dev tools; check the `prediction` events arriving via `@attenlabs/saa-js`. Threshold may need tuning per environment. |
| PSTN call drops | Twilio's webhook target must be public HTTPS; use `make tunnel` for dev. |

## See also

- [`packages/saa-js/README.md`](../../packages/saa-js/README.md): SDK reference.
- [`packages/saa-gate/README.md`](../../packages/saa-gate/README.md): the cross-framework routing policy.
- [`examples/proactive-agent/elevenlabs-cai/`](../proactive-agent/elevenlabs-cai/): the proactive overlay built on this adapter.
- [`examples/README.md`](../README.md): adapter index.


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
