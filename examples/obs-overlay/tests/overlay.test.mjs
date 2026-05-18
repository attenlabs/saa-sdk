// node:test suite for @attenlabs/saa-overlay. Uses a tiny fake DOM so tests
// run during pack smoke without jsdom or browser dependencies.

import test, { beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { mount } from '../src/overlay.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  contains(value) { return this.values.has(value); }
}

class FakeElement {
  constructor(ownerDocument = null) {
    this.ownerDocument = ownerDocument;
    this.innerHTML = '';
    this.attributes = new Map();
    this.classList = new FakeClassList();
  }
  setAttribute(key, value) { this.attributes.set(key, String(value)); }
  getAttribute(key) { return this.attributes.has(key) ? this.attributes.get(key) : null; }
  removeAttribute(key) { this.attributes.delete(key); }
}

function installDom() {
  const body = new FakeElement();
  const root = new FakeElement();
  const doc = {
    body,
    querySelector(selector) { return selector === '#root' ? root : null; },
  };
  body.ownerDocument = doc;
  root.ownerDocument = doc;
  globalThis.document = doc;
  globalThis.window = globalThis;
  globalThis.HTMLElement = FakeElement;
  delete globalThis.requestAnimationFrame;
  return { root, body, doc };
}

beforeEach(() => {
  installDom();
});

afterEach(() => {
  delete globalThis.document;
  delete globalThis.window;
  delete globalThis.HTMLElement;
  delete globalThis.requestAnimationFrame;
  delete globalThis.EventSource;
  delete globalThis.WebSocket;
});

const evt = (decision, command, opts = {}) => ({
  id: opts.id,
  traceId: opts.traceId,
  spanId: opts.spanId,
  ts: new Date(opts.at ?? Date.now()).toISOString(),
  decision,
  command,
  confidence: opts.confidence,
  latencyMs: opts.latencyMs,
  source: opts.source,
  ...(opts.rule ? { ruleId: opts.rule } : {}),
  ...(opts.reason ? { reason: opts.reason } : {}),
});

test('empty mount sets ARIA, theme, status row, and idle row', () => {
  const root = document.querySelector('#root');
  const handle = mount({ container: '#root', theme: 'dark', renderScheduler: 'sync' });

  assert.equal(root.getAttribute('role'), 'log');
  assert.equal(root.getAttribute('aria-live'), 'polite');
  assert.equal(root.ownerDocument.body.getAttribute('data-theme'), 'dark');
  assert.match(root.innerHTML, /SAA decision flight recorder|audit log|push\(\) ready/);
  assert.equal(handle.getStatus().state, 'idle');

  handle.unmount();
});

test('mount throws when the container cannot be resolved', () => {
  assert.throws(() => mount({ container: '#missing' }), /container not found/);
});

test('renders decision metadata, redacts secrets, and tracks stats', () => {
  const root = document.querySelector('#root');
  const handle = mount({ container: '#root', renderScheduler: 'sync', showStatus: false });
  handle.push([
    evt('pass', 'npm test', { id: '1', confidence: 0.81, latencyMs: 12, source: 'tiny' }),
    evt('drop', 'curl https://x.test?token=abc -H "Bearer sk-12345678901234567890"', {
      id: '2',
      rule: 'policy.secret',
      reason: 'token leak',
      traceId: 'trace-1',
    }),
  ]);

  assert.match(root.innerHTML, /ol-pill-pass/);
  assert.match(root.innerHTML, /ol-pill-drop/);
  assert.match(root.innerHTML, /81%/);
  assert.match(root.innerHTML, /12ms/);
  assert.match(root.innerHTML, /policy\.secret/);
  assert.match(root.innerHTML, /\[redacted\]/);
  assert.doesNotMatch(root.innerHTML, /sk-123456/);
  assert.equal(handle.getStats().acceptedEvents, 2);
  assert.equal(handle.getStats().decisionCounts.drop, 1);
});

test('rolling window trims entries relative to newest timestamp', () => {
  const handle = mount({ container: '#root', renderScheduler: 'sync', windowMs: 30_000, showStatus: false });
  const t = Date.now();
  handle.push([
    evt('pass', 'old', { at: t - 45_000 }),
    evt('abstain', 'middle', { at: t - 10_000 }),
    evt('drop', 'fresh', { at: t }),
  ]);

  assert.deepEqual(handle.getEntries().map((e) => e.command), ['middle', 'fresh']);
  assert.equal(handle.getStats().trimmedEntries, 1);
  assert.doesNotMatch(document.querySelector('#root').innerHTML, />old</);
});

test('wrapped HookDecisionPayload form renders like flat events', () => {
  const root = document.querySelector('#root');
  const handle = mount({ container: '#root', renderScheduler: 'sync', showStatus: false });
  handle.push({
    ts: new Date().toISOString(),
    type: 'hookDecision',
    payload: {
      decision: {
        decision: 'override',
        ruleId: 'rule.user.override',
        command: 'git push --force',
        reason: 'maintainer ack',
        confidence: 0.99,
      },
    },
  });

  assert.match(root.innerHTML, /ol-pill-override/);
  assert.match(root.innerHTML, /rule\.user\.override/);
  assert.match(root.innerHTML, /git push --force/);
  assert.match(root.innerHTML, /99%/);
});

test('maxEntries is a hard memory cap and visibleRows is a render cap', () => {
  const root = document.querySelector('#root');
  const handle = mount({ container: '#root', renderScheduler: 'sync', maxEntries: 5, visibleRows: 2, showStatus: false });
  const base = Date.now();
  for (let i = 0; i < 9; i += 1) handle.push(evt('pass', `cmd-${i}`, { at: base + i * 10 }));

  assert.equal(handle.getEntries().length, 5);
  assert.equal(handle.getStats().droppedEntries, 4);
  assert.match(root.innerHTML, /cmd-7/);
  assert.match(root.innerHTML, /cmd-8/);
  assert.doesNotMatch(root.innerHTML, /cmd-6\b/);
});

test('dedupe rejects duplicate ids without growing the buffer', () => {
  const handle = mount({ container: '#root', renderScheduler: 'sync', showStatus: false });
  handle.push(evt('pass', 'first', { id: 'dup' }));
  handle.push(evt('drop', 'second', { id: 'dup' }));

  assert.equal(handle.getEntries().length, 1);
  assert.equal(handle.getEntries()[0].command, 'first');
  assert.equal(handle.getStats().dedupedEvents, 1);
});

test('decision and confidence filters keep full telemetry but hide rows', () => {
  const root = document.querySelector('#root');
  const handle = mount({
    container: '#root',
    renderScheduler: 'sync',
    showStatus: false,
    decisions: ['drop'],
    minConfidence: 0.7,
  });
  handle.push([
    evt('pass', 'hidden-pass', { confidence: 0.9 }),
    evt('drop', 'hidden-low-confidence', { confidence: 0.2 }),
    evt('drop', 'visible-drop', { confidence: 0.8 }),
  ]);

  assert.equal(handle.getEntries().length, 3);
  assert.equal(handle.getVisibleEntries().length, 1);
  assert.match(root.innerHTML, /visible-drop/);
  assert.doesNotMatch(root.innerHTML, /hidden-pass/);
  assert.doesNotMatch(root.innerHTML, /hidden-low-confidence/);
  assert.equal(handle.getStats().filterRejectedEvents, 2);
});

test('setFilter can change the visible slice after ingest', () => {
  const root = document.querySelector('#root');
  const handle = mount({ container: '#root', renderScheduler: 'sync', showStatus: false });
  handle.push([evt('pass', 'keep'), evt('drop', 'hide')]);
  handle.setFilter((entry) => entry.decision === 'pass');

  assert.match(root.innerHTML, /keep/);
  assert.doesNotMatch(root.innerHTML, /hide/);
});

test('pause/resume keeps ingesting but suppresses scheduled renders while paused', () => {
  const root = document.querySelector('#root');
  const handle = mount({ container: '#root', renderScheduler: 'sync' });
  handle.pause();
  assert.match(root.innerHTML, /paused/);
  handle.push(evt('drop', 'queued-while-paused'));
  assert.equal(handle.getEntries().length, 1);
  assert.doesNotMatch(root.innerHTML, /queued-while-paused/);
  handle.resume();
  assert.match(root.innerHTML, /queued-while-paused/);
});

test('raf scheduler coalesces many pushes into one render', () => {
  const root = document.querySelector('#root');
  const rafs = [];
  globalThis.requestAnimationFrame = (fn) => { rafs.push(fn); return rafs.length; };
  const handle = mount({ container: '#root', renderScheduler: 'raf', showStatus: false });
  const initialRenders = handle.getStats().renderCount;

  handle.push(evt('pass', 'coalesce-1'));
  handle.push(evt('drop', 'coalesce-2'));
  assert.equal(rafs.length, 1);
  assert.doesNotMatch(root.innerHTML, /coalesce-2/);
  rafs.shift()();

  assert.match(root.innerHTML, /coalesce-2/);
  assert.equal(handle.getStats().renderCount, initialRenders + 1);
  assert.equal(handle.getStats().scheduledRenderCount, 1);
});

test('EventSource-shaped source ingests frames, counts malformed frames, and closes', () => {
  let onmessage;
  let closed = false;
  const fakeEs = {
    close() { closed = true; },
    set onmessage(fn) { onmessage = fn; },
    get onmessage() { return onmessage; },
    set onopen(_fn) {},
    set onerror(_fn) {},
  };
  const handle = mount({ container: '#root', source: fakeEs, renderScheduler: 'sync', showStatus: false });
  onmessage({ data: JSON.stringify([evt('drop', 'sse-array')]) });
  onmessage({ data: '{not json' });

  assert.match(document.querySelector('#root').innerHTML, /sse-array/);
  assert.equal(handle.getStats().frames, 2);
  assert.equal(handle.getStats().malformedFrames, 1);
  handle.unmount();
  assert.equal(closed, true);
});

test('WebSocket-shaped source updates status and ingests frames', () => {
  let onmessage;
  let onclose;
  let closed = false;
  const fakeWs = {
    send() {},
    close() { closed = true; onclose?.(); },
    set onmessage(fn) { onmessage = fn; },
    get onmessage() { return onmessage; },
    set onclose(fn) { onclose = fn; },
    get onclose() { return onclose; },
    set onopen(_fn) {},
    set onerror(_fn) {},
  };
  const handle = mount({ container: '#root', source: fakeWs, renderScheduler: 'sync' });
  onmessage({ data: JSON.stringify(evt('pass', 'ws-fed')) });

  assert.match(document.querySelector('#root').innerHTML, /ws-fed/);
  assert.equal(handle.getStatus().transport, 'websocket');
  handle.unmount();
  assert.equal(closed, true);
});

test('10k event burst remains bounded, drains the queue, and renders only visibleRows', async () => {
  const root = document.querySelector('#root');
  const handle = mount({ container: '#root', renderScheduler: 'sync', showStatus: false, maxEntries: 256, visibleRows: 3 });
  const t = Date.now();
  const burst = Array.from({ length: 10_000 }, (_, i) => evt(i % 2 ? 'pass' : 'drop', `burst-${i}`, { at: t + i }));
  handle.push(burst);
  assert.equal(handle.getStats().pendingEvents > 0, true);
  await handle.drain();
  handle.flush();

  const stats = handle.getStats();
  assert.equal(stats.acceptedEvents, 10_000);
  assert.equal(handle.getEntries().length, 256);
  assert.equal(stats.droppedEntries, 9_744);
  assert.match(root.innerHTML, /burst-9999/);
  assert.doesNotMatch(root.innerHTML, /burst-9996\b/);
});


test('oversized frames are rejected before JSON parse', () => {
  let onmessage;
  const fakeEs = {
    close() {},
    set onmessage(fn) { onmessage = fn; },
    get onmessage() { return onmessage; },
    set onopen(_fn) {},
    set onerror(_fn) {},
  };
  const handle = mount({ container: '#root', source: fakeEs, renderScheduler: 'sync', maxFrameBytes: 20 });
  onmessage({ data: JSON.stringify(evt('drop', 'this frame is deliberately too large')) });

  const stats = handle.getStats();
  assert.equal(stats.oversizedFrames, 1);
  assert.equal(stats.acceptedEvents, 0);
  assert.match(stats.lastError, /oversized frame/);
  assert.equal(handle.getHealth().ok, false);
});

test('large frames are capped and preserve newest events', () => {
  const root = document.querySelector('#root');
  const handle = mount({
    container: '#root',
    renderScheduler: 'sync',
    showStatus: false,
    maxFrameEvents: 3,
    ingestBatchSize: 100,
  });
  handle.push([
    evt('pass', 'old-1'),
    evt('pass', 'old-2'),
    evt('drop', 'keep-1'),
    evt('drop', 'keep-2'),
    evt('drop', 'keep-3'),
  ]);

  assert.equal(handle.getStats().frameEventsDropped, 2);
  assert.deepEqual(handle.getEntries().map((entry) => entry.command), ['keep-1', 'keep-2', 'keep-3']);
  assert.doesNotMatch(root.innerHTML, /old-1|old-2/);
});

test('backpressure drops oldest pending events and keeps the newest telemetry', async () => {
  const root = document.querySelector('#root');
  const handle = mount({
    container: '#root',
    renderScheduler: 'sync',
    showStatus: false,
    maxFrameEvents: 100,
    maxPendingEvents: 4,
    ingestBatchSize: 1,
    batchScheduler: 'timeout',
  });
  handle.push(Array.from({ length: 8 }, (_, i) => evt('pass', `queued-${i}`)));

  assert.equal(handle.getStats().frameEventsDropped, 0);
  assert.equal(handle.getStats().backpressureDroppedEvents, 4);
  await handle.drain();
  handle.flush();

  assert.deepEqual(handle.getEntries().map((entry) => entry.command), ['queued-4', 'queued-5', 'queued-6', 'queued-7']);
  assert.match(root.innerHTML, /queued-7/);
  assert.doesNotMatch(root.innerHTML, /queued-0/);
});


test('package metadata, docs, and schema are coherent', () => {
  const pkg = JSON.parse(readFileSync(resolve(ROOT, 'package.json'), 'utf8'));
  assert.equal(pkg.name, '@attenlabs/saa-overlay');
  assert.equal(pkg.version, '0.4.0');
  assert.equal(pkg.types, './dist/saa-overlay.d.ts');
  assert.deepEqual(pkg.peerDependencies, {});
  assert.deepEqual(pkg.devDependencies, {});

  const events = readFileSync(resolve(ROOT, 'EVENTS.md'), 'utf8');
  assert.match(events, /decision flight recorder/i);
  assert.match(events, /renderScheduler/i);

  const schema = JSON.parse(readFileSync(resolve(ROOT, 'schemas', 'event.schema.json'), 'utf8'));
  const encoded = JSON.stringify(schema);
  assert.match(encoded, /saa-trace\.schema\.json/);
  assert.match(encoded, /HookDecisionPayload/);
});
