<p align="center">
  <a href="../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# Production gate example

This example shows the production shape SAA should ship with:

```text
browser mic/camera
  -> @attenlabs/saa-js
  -> SAA Cloud (WebSocket)
  -> speechReady
  -> @attenlabs/saa-gate
  -> downstream STT/realtime agent only when addressed
```

It includes two pieces:

- `token-server.mjs`: a zero-dependency Node token broker. It keeps `sk_live_*` on the server and mints short-lived session tokens for browsers.
- `browser-main.js`: a browser skeleton that attaches `@attenlabs/saa-gate` to `@attenlabs/saa-js` and routes allowed speech into a downstream realtime agent.

## Run the token broker

```bash
export SAA_API_KEY=sk_live_replace_me
export SAA_PROJECT_ID=prj_replace_me
export SAA_ALLOWED_ORIGINS=http://localhost:5173,https://app.example.com
node examples/production-gate/token-server.mjs
```

Endpoints:

```text
GET  /healthz
GET  /metrics
POST /v1/saa/session
```

The browser calls `POST /v1/saa/session` and receives:

```json
{
  "token": "short_lived_session_token",
  "expires_at": "2026-05-14T12:00:00.000Z",
  "id": "tok_...",
  "ws_url": "wss://server.attentionlabs.ai/ws"
}
```

## What the broker enforces

Defaults set by [`token-server.mjs`](./token-server.mjs); each is configurable via env or `createTokenBrokerServer({...})`:

| Control | Default | Override |
|---|---|---|
| Origin allow-list | empty (every request 403s) | `SAA_ALLOWED_ORIGINS` (comma-separated origins) |
| Per-IP rate limit | 30 requests / 60 s | `RATE_LIMIT_MAX`, `RATE_LIMIT_WINDOW_MS` |
| Token TTL | 60 s (min 30 s, max 300 s) | `ttl_seconds` in the POST body |
| Request body cap | 4 KB | not configurable |

`sk_live_*` and `SAA_PROJECT_ID` stay server-side; the control plane never sees the browser. The broker rejects same-origin and cross-origin requests whose `Origin` header isn't on the allow-list, so it works behind any reverse-proxy topology (same-origin path mount, dedicated `broker.example.com`, etc.). Deploy CORS preflight / `Access-Control-Allow-Origin` on the reverse proxy if browsers will cross-origin to the broker.

The broker reads `req.socket.remoteAddress` for the rate-limit key. Behind a reverse proxy that's the proxy IP; rely on the proxy's own per-client limit (or parse `X-Forwarded-For` before passing the request along).

## Integration rule

Do not stream raw room audio into STT/LLM by default. Route only `speechReady.audioBase64` unless a customer explicitly chooses a fail-open mode.

## OpenAI Realtime note

The helper in `openai-realtime-bridge.js` sends:

```text
input_audio_buffer.append
input_audio_buffer.commit
response.create
```

Use it when your Realtime session is configured for manual audio-buffer control, or when automatic response creation is disabled and you create responses manually.

## See also

- [`packages/saa-js/README.md`](../../packages/saa-js/README.md): SDK reference.
- [`packages/saa-gate/README.md`](../../packages/saa-gate/README.md): the routing policy state machine this skeleton wraps.
- [`examples/cloud-live-demo/`](../cloud-live-demo/): the simplest browser demo (just `AttentionClient` + visual readout, no gate, no broker); the natural starting point before this example.
- [`examples/README.md`](../README.md): adapter index.


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
