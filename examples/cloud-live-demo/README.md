<p align="center">
  <a href="../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# cloud-live-demo

The canonical minimal browser demo for SAA Cloud. The simplest first thing to run.

| | |
|---|---|
| Run             | `npx serve examples/cloud-live-demo -p 8081` and open <http://localhost:8081> |
| Demonstrates    | A bare `AttentionClient` + visual readout: prediction class + confidence, RTT badge, face count, VAD probability, conversation state, audio waveform. |
| Expect          | Click **Start**, grant mic + camera, watch the prediction pill flip between `silent` / `human-directed` / `device-directed`. |
| Known gotcha    | The page expects a short-lived JWT from `/api/demo-token` (same origin). Without it, the dashboard-token fallback banner renders with a CTA to mint a free token at [attentionlabs.ai/dashboard](https://attentionlabs.ai/dashboard). The demo never falls back to a fake or local prediction stream. |

## What the page does

1. POSTs to `/api/demo-token` and expects a short-lived JWT back.
2. Streams mic + webcam to `wss://server.attentionlabs.ai/ws` for up to 60 s.
3. Renders live: prediction class + confidence + threshold marker, RTT badge, face count, VAD probability, conversation state, a sibling-`AnalyserNode` waveform overlaid on the video.
4. Hard-stops at the timeout and surfaces a CTA back to the dashboard.

Point the page at a different demo-token host with a URL hash or meta tag:

```
http://localhost:8081#api=https://attentionlabs.ai
```

```html
<meta name="saa-api-base" content="https://attentionlabs.ai">
```

The demo-token response may also override the WebSocket URL via a `ws_url` field.

## Server contract: `POST /api/demo-token`

The endpoint is operated by the production service, not by this repo. The contract:

**200, success**

```json
{
  "token": "<short-lived JWT>",
  "expires_in_sec": 60,
  "ws_url": "wss://server.attentionlabs.ai/ws"
}
```

- `token` must be accepted by the SAA WebSocket subprotocol negotiation.
- `expires_in_sec` must be ≤ 60 in production. The client clamps to `[5, 120]` defensively.
- `ws_url` is optional; defaults to `wss://server.attentionlabs.ai/ws`.

**429, rate limited**: `{ "retry_after_sec": 300, "reason": "rate-limited" }`

**503 / `{ ready: false }`**: capacity full or temporarily disabled.

### Safeguards the server enforces

- Per-IP rate limit (e.g. 1 token per IP per 5 minutes).
- Per-token concurrency cap (1 active WebSocket; second open is rejected).
- Global demo concurrency cap to protect dashboard customers.
- Server-side kill at `expires_in_sec` (close code 1008 + reason `demo_token_expired`).
- One-shot issuance: reusing a consumed token closes with 1008.

The public demo bundle is by definition untrusted; long-lived tokens never live in it. See [`SECURITY.md`](../../SECURITY.md).

## What this demo does not do

- **No client-side learned variant.** SAA's open SDKs stream to the cloud. The classifier is not released.
- **No browser-side face tracker.** The face badge uses the server-emitted `numFaces` field.
- **No automatic retry on rate-limit.** If the server says "slow down," the page slows down.

## Files

- [`index.html`](./index.html): page shell + inline CSS.
- [`main.js`](./main.js): token preflight + `AttentionClient` wiring + waveform + countdown.

## Smoke test

```bash
node test_smoke_shape.mjs
```

## See also

- [`packages/saa-js/README.md`](../../packages/saa-js/README.md): SDK reference.
- [`examples/production-gate/`](../production-gate/): the policy-gated browser skeleton, the natural next step from this demo.
- [`examples/README.md`](../README.md): adapter index.


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
