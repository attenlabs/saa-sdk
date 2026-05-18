# Changelog

## 0.4.0

- Added production hardening for bounded input handling: `maxFrameBytes`, `maxFrameEvents`, `maxPendingEvents`, `ingestBatchSize`, and `batchScheduler`.
- Added bounded asynchronous ingestion for large event arrays plus `drain()` for tests and deterministic demos.
- Added `getHealth()` with queue depth, stale-frame age, transport status, and last error.
- Added tests for oversized-frame rejection, frame event caps, and queue backpressure.

## 0.3.0

Sharp-point rewrite: decision flight recorder, not decorative pill widget.

- Added bounded ring-buffer storage with `maxEntries` hard cap.
- Added render coalescing via `renderScheduler: 'raf' | 'microtask' | 'timeout' | 'sync'`.
- Added `flush()`, `setFilter()`, `setTheme()`, `getVisibleEntries()`, `toJSON()`, and `exportJSON()`.
- Added decision/confidence/custom filters while retaining full telemetry in the buffer.
- Added WebSocket URL reconnect with exponential backoff and jitter.
- Added transport status row and richer lifecycle status.
- Added default redaction, text caps, id/trace/span normalization, confidence, latency, and source metadata.
- Added render/performance stats and a local `npm run bench` guardrail.
- Removed the jsdom dev dependency; tests now run with a tiny fake DOM.
- Added generated TypeScript declarations.

## 0.1.0

Initial package candidate.

- Drop-in HTML/CSS overlay for PASS / DROP / ABSTAIN / OVERRIDE decision pills.
- SSE, WebSocket, and direct `push()` input.
- Flat and wrapped HookDecisionPayload-compatible event forms.
- Rolling 30 s visible log, OBS / dark / light themes, and zero runtime dependencies.
