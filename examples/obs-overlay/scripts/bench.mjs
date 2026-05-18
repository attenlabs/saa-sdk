#!/usr/bin/env node
// Local guardrail for burst ingest and bounded rendering. This is a fake-DOM
// package benchmark, not a browser lab benchmark.

import { mount } from '../src/overlay.js';

class FakeElement {
  constructor(ownerDocument = null) {
    this.ownerDocument = ownerDocument;
    this.innerHTML = '';
    this.attributes = new Map();
  }
  setAttribute(key, value) { this.attributes.set(key, String(value)); }
  getAttribute(key) { return this.attributes.has(key) ? this.attributes.get(key) : null; }
  removeAttribute(key) { this.attributes.delete(key); }
}

const body = new FakeElement();
const root = new FakeElement();
const doc = { body, querySelector: (selector) => (selector === '#root' ? root : null) };
body.ownerDocument = doc;
root.ownerDocument = doc;
globalThis.document = doc;

const rafQueue = [];
globalThis.requestAnimationFrame = (fn) => { rafQueue.push(fn); return rafQueue.length; };

const count = Number(process.argv[2] ?? 10_000);
const start = Date.now();
const events = Array.from({ length: count }, (_, i) => ({
  id: `bench-${i}`,
  ts: new Date(start + i).toISOString(),
  decision: i % 97 === 0 ? 'override' : (i % 2 ? 'pass' : 'drop'),
  ruleId: i % 2 ? 'bench.pass' : 'bench.drop',
  command_preview: `bench command ${i}`,
  confidence: (i % 100) / 100,
  latencyMs: i % 31,
  source: 'bench',
}));

const overlay = mount({
  container: '#root',
  showStatus: false,
  maxEntries: 512,
  visibleRows: 3,
  renderScheduler: 'raf',
});

const t0 = performance.now();
overlay.push(events);
overlay.flush();
const t1 = performance.now();

const stats = overlay.getStats();
const rowsRendered = (root.innerHTML.match(/class="ol-row/g) ?? []).length;
const result = {
  inputEvents: count,
  acceptedEvents: stats.acceptedEvents,
  storedEntries: overlay.getEntries().length,
  renderedRows: rowsRendered,
  droppedEntries: stats.droppedEntries,
  renderCount: stats.renderCount,
  lastRenderDurationMs: Number(stats.lastRenderDurationMs.toFixed(3)),
  maxRenderDurationMs: Number(stats.maxRenderDurationMs.toFixed(3)),
  totalMs: Number((t1 - t0).toFixed(3)),
  htmlBytes: root.innerHTML.length,
};

console.log(JSON.stringify(result, null, 2));

if (overlay.getEntries().length > 512) throw new Error('maxEntries cap violated');
if (rowsRendered > 3) throw new Error('visibleRows cap violated');
if (stats.acceptedEvents !== count) throw new Error('accepted event count mismatch');
