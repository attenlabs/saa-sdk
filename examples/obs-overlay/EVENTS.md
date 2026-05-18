# SAA overlay event contract

`@attenlabs/saa-overlay` consumes decision events and renders them as a live decision flight recorder. The package intentionally accepts a forgiving input contract so demos, SDK sidecars, and trace-schema emitters can feed the same surface.

## Inputs

| Source | Payload | Notes |
|---|---|---|
| EventSource / SSE URL | one JSON event or JSON array per `message` | EventSource owns retry; overlay reports status/errors. |
| WebSocket URL | one JSON event or JSON array per text frame | Overlay reconnects with exponential backoff unless `reconnect: false`. |
| WebSocket instance | one JSON event or JSON array per text frame | No reconnect because the caller owns the instance. |
| `push()` | object or array | Small payloads ingest synchronously; large arrays are queued and can be awaited with `drain()`. |

## Preferred flat event

```json
{
  "id": "evt_123",
  "traceId": "trace_abc",
  "spanId": "span_def",
  "ts": "2026-05-14T20:10:00.000Z",
  "decision": "drop",
  "ruleId": "policy.secret",
  "command_preview": "curl https://api.example.test?token=[redacted]",
  "reason": "secret-like query parameter",
  "confidence": 0.94,
  "latencyMs": 12,
  "source": "saa-cloud"
}
```

## Wrapped trace-schema form

```json
{
  "type": "hookDecision",
  "payload": {
    "decision": {
      "decision": "pass",
      "ruleId": "rule.addressee",
      "command": "send assistant response",
      "reason": "wake phrase and speaker match"
    }
  }
}
```

The wrapped shape mirrors a generic decision payload (`payload.decision.*`); the overlay accepts both flat events and wrapped envelopes.

## Decision enum

Renderable decisions are:

- `pass`
- `drop`
- `abstain`
- `override`
- `idle`

`idle` is a rendering-only visual state. It should not appear as a model or audit decision unless a producer intentionally sends a quiescent UI event.

## Field aliases

| Normalized field | Accepted aliases |
|---|---|
| `id` | `id`, `eventId`, `payload.id`, `payload.eventId`, `payload.decision.id` |
| `traceId` | `traceId`, `trace_id`, `payload.traceId`, `payload.decision.traceId` |
| `spanId` | `spanId`, `span_id`, `payload.spanId`, `payload.decision.spanId` |
| `ts` | `ts`, `timestamp`, `createdAt`, `time`, `payload.ts`, `payload.timestamp` |
| `decision` | `decision`, `payload.decision.decision`, `payload.decision` |
| `rule` | `ruleId`, `rule`, `rule_id`, `payload.decision.ruleId`, `payload.ruleId` |
| `command` | `command_preview`, `commandPreview`, `command`, `payload.decision.command` |
| `reason` | `reason`, `payload.decision.reason`, `payload.reason`, `explanation` |
| `confidence` | `confidence`, `score`, `payload.decision.confidence`, `payload.score` |
| `latencyMs` | `latencyMs`, `latency_ms`, `durationMs`, `duration_ms` |
| `source` | `source`, `adapter`, `provider`, `payload.source`, `payload.decision.source` |

## Buffering and renderScheduler

Defaults:

| Option | Default | Meaning |
|---|---:|---|
| `windowMs` | `30000` | Rolling timestamp horizon. |
| `visibleRows` | `3` | Rows painted on screen, excluding status. |
| `maxEntries` | `512` | Hard memory cap for normalized entries. |
| `maxTextLength` | `180` | Per-field display cap before HTML escaping. |
| `maxFrameBytes` | `256000` | Maximum text-frame size before parse rejection. |
| `maxFrameEvents` | `10000` | Maximum events accepted from one parsed array; newest events are kept. |
| `maxPendingEvents` | `10000` | Maximum queued events awaiting batch processing. |
| `ingestBatchSize` | `500` | Maximum queued events processed in one batch. |
| `batchScheduler` | `timeout` | Queue scheduler: `timeout`, `microtask`, or `sync`. |
| `renderScheduler` | `raf` | Coalesces paints through `requestAnimationFrame` where available. |

Storage is bounded by `maxEntries`; rendering is bounded by `visibleRows`; frame ingestion is bounded by `maxFrameBytes`, `maxFrameEvents`, and `maxPendingEvents`. Events without timestamps remain until the memory cap is reached or `clear()` is called.

## Redaction

Redaction is enabled by default because OBS and dashboard overlays are often recorded. The default redactor masks common bearer tokens, JWT-looking strings, `sk-...` style keys, AWS access-key-looking values, token-like environment assignments, and sensitive URL query parameters.

Producers should still prefer `command_preview` / `commandPreview` and avoid sending raw secrets to the browser. Set `redact: false` only for trusted local debugging.
