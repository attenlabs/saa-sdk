#!/usr/bin/env node
// Build dist/saa-overlay.esm.js, dist/saa-overlay.umd.cjs, and
// dist/saa-overlay.d.ts from src/overlay.js. No deps; Node 20+ in release.

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const DIST = resolve(ROOT, 'dist');
mkdirSync(DIST, { recursive: true });

const source = readFileSync(resolve(ROOT, 'src', 'overlay.js'), 'utf8');
const banner =
  '// @attenlabs/saa-overlay — generated bundle. Do not edit by hand.\n' +
  '// Source of truth: packages/saa-overlay/src/overlay.js.\n\n';

writeFileSync(resolve(DIST, 'saa-overlay.esm.js'), banner + source);

const umdBody = source
  .replace(/^export function /gm, 'function ')
  .replace(/^export const /gm, 'const ')
  .replace(/^export default .*$/gm, '')
  .replace(/^export \{[^}]*\};?\s*$/gm, '')
  .replace(/\n\/\/ Browser global side[- ]effect:[\s\S]*$/m, '\n');

writeFileSync(
  resolve(DIST, 'saa-overlay.umd.cjs'),
  banner +
    `(function (root, factory) {
  if (typeof exports === 'object' && typeof module !== 'undefined') module.exports = factory();
  else if (typeof define === 'function' && define.amd) define([], factory);
  else root.SaaOverlay = factory();
}(typeof self !== 'undefined' ? self : this, function () {
${umdBody}
  return SaaOverlay;
}));
`,
);

writeFileSync(
  resolve(DIST, 'saa-overlay.d.ts'),
  `export type SaaDecision = 'pass' | 'drop' | 'abstain' | 'override' | 'idle';
export type SaaOverlayTheme = 'dark' | 'light' | 'obs';
export type SaaOverlayScheduler = 'raf' | 'microtask' | 'timeout' | 'sync';
export type SaaOverlayBatchScheduler = 'microtask' | 'timeout' | 'sync';
export type SaaOverlayRedactor = false | boolean | ((text: string) => string);

export interface SaaOverlayReconnectOptions {
  enabled?: boolean;
  retries?: number;
  baseMs?: number;
  maxMs?: number;
  jitterMs?: number;
}

export interface SaaOverlayEntry {
  id: string;
  sequence: number;
  ts: string;
  tsMs: number | null;
  clock: string;
  decision: SaaDecision;
  rule: string;
  command: string;
  reason: string;
  traceId: string;
  spanId: string;
  confidence: number | null;
  latencyMs: number | null;
  source: string;
  renderVisible: boolean;
}

export interface SaaOverlayStats {
  frames: number;
  acceptedEvents: number;
  ignoredEvents: number;
  malformedFrames: number;
  oversizedFrames: number;
  frameEventsDropped: number;
  dedupedEvents: number;
  filterRejectedEvents: number;
  trimmedEntries: number;
  droppedEntries: number;
  backpressureDroppedEvents: number;
  queuedEvents: number;
  pendingEvents: number;
  maxPendingEventsSeen: number;
  processedBatches: number;
  reconnects: number;
  renderCount: number;
  scheduledRenderCount: number;
  lastFrameAt: number | null;
  lastEventAt: number | null;
  lastRenderAt: number | null;
  lastRenderDurationMs: number;
  maxRenderDurationMs: number;
  avgRenderDurationMs: number;
  lastError: string;
  decisionCounts: Record<SaaDecision, number>;
  eventsPerSecond: number;
}

export interface SaaOverlayHealth {
  ok: boolean;
  state: SaaOverlayStatus['state'];
  transport: SaaOverlayStatus['transport'];
  staleMs: number | null;
  pendingEvents: number;
  acceptedEvents: number;
  lastError: string;
}

export interface SaaOverlayStatus {
  state: 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'paused' | 'closed' | 'error';
  transport: 'manual' | 'stream' | 'eventsource' | 'websocket' | string;
  message: string;
  reconnectAttempt: number;
  paused: boolean;
}

export interface SaaOverlayEventEnvelope {
  type: 'entry' | 'status' | 'render';
  entry?: SaaOverlayEntry;
  status?: SaaOverlayStatus;
  stats?: SaaOverlayStats;
  durationMs?: number;
  visibleRows?: number;
}

export interface SaaOverlayMountOptions {
  container?: HTMLElement | string;
  source?: string | EventSource | WebSocket;
  theme?: SaaOverlayTheme;
  windowMs?: number;
  visibleRows?: number;
  maxEntries?: number;
  maxTextLength?: number;
  showStatus?: boolean;
  dedupe?: boolean;
  redact?: SaaOverlayRedactor;
  renderScheduler?: SaaOverlayScheduler;
  sort?: boolean;
  maxSeenIds?: number;
  maxFrameBytes?: number;
  maxFrameEvents?: number;
  maxPendingEvents?: number;
  ingestBatchSize?: number;
  batchScheduler?: SaaOverlayBatchScheduler;
  decisions?: Iterable<SaaDecision | string>;
  minConfidence?: number;
  filter?: (entry: SaaOverlayEntry, raw: unknown) => boolean;
  reconnect?: false | SaaOverlayReconnectOptions;
  onEvent?: (entry: SaaOverlayEntry, stats: SaaOverlayStats) => void;
  onStatus?: (status: SaaOverlayStatus) => void;
  onRender?: (render: { durationMs: number; visibleRows: number; stats: SaaOverlayStats }) => void;
}

export interface SaaOverlayHandle {
  unmount(): void;
  push(eventOrArray: unknown): void;
  clear(): void;
  pause(): void;
  resume(): void;
  flush(): void;
  drain(): Promise<void>;
  subscribe(fn: (event: SaaOverlayEventEnvelope) => void): () => void;
  setFilter(filter?: ((entry: SaaOverlayEntry, raw: unknown) => boolean) | null): void;
  setTheme(theme: SaaOverlayTheme): void;
  getEntries(): SaaOverlayEntry[];
  getVisibleEntries(): SaaOverlayEntry[];
  getStats(): SaaOverlayStats;
  getStatus(): SaaOverlayStatus;
  getHealth(): SaaOverlayHealth;
  toJSON(): { entries: SaaOverlayEntry[]; stats: SaaOverlayStats; status: SaaOverlayStatus; health: SaaOverlayHealth };
  exportJSON(): string;
}

export function mount(opts?: SaaOverlayMountOptions): SaaOverlayHandle;
declare const SaaOverlay: { mount: typeof mount };
export { SaaOverlay };
export default SaaOverlay;
`,
);

console.log('built dist/saa-overlay.esm.js, dist/saa-overlay.umd.cjs, and dist/saa-overlay.d.ts');
