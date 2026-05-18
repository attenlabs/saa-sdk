<!-- saa-claims-allow: explicitly negates SOTA framing ("should never claim ML SOTA") -->
# Performance posture

The realistic SOTA target for `@attenlabs/saa-overlay` is not model accuracy. It is runtime discipline for a tiny browser overlay:

1. bounded memory,
2. capped paint work,
3. coalesced burst rendering,
4. bounded frame parsing and pending-queue backpressure,
5. no runtime dependencies,
6. privacy-preserving display by default.

## Invariants

| Invariant | Implementation |
|---|---|
| Storage cannot grow forever | Ring buffer capped by `maxEntries`. |
| Paint work is capped | Render path slices to `visibleRows`. |
| Burst input does not paint per event | Default `renderScheduler: 'raf'` coalesces paints. |
| Incoming text frames are capped | `maxFrameBytes` rejects oversized frames before JSON parse. |
| Large JSON arrays cannot run unbounded | `maxFrameEvents` caps per-frame events and keeps the newest telemetry. |
| Pending input cannot grow forever | `maxPendingEvents` bounds the queue; oldest pending events drop first under pressure. |
| Large bursts yield across ticks | `ingestBatchSize` + `batchScheduler` process queued events in bounded batches. |
| Old timestamped entries expire | `windowMs` trim relative to newest event timestamp. |
| Secrets should not appear in recorded overlays | Default display redactor and per-field text caps. |
| Runtime dependency risk is zero | No runtime dependencies and no dev dependencies. |

## Local guardrail

```sh
npm run bench
```

The benchmark feeds 10,000 synthetic events into a fake DOM, drains the bounded ingest queue, flushes one render, and reports accepted events, stored entries, rendered rows, dropped entries, and render timing. This is not a substitute for browser profiling, but it prevents the package from regressing into per-event DOM painting or unbounded memory.

## Honest limitation

This package should never claim ML SOTA. Its measurable claim is operational: tiny, bounded, redacted, and fast enough for live decision visibility.
