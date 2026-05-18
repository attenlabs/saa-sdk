// @attenlabs/saa-overlay — bounded high-throughput decision flight recorder.
// Canonical ESM source. Build emits dist/saa-overlay.{esm.js,umd.cjs,d.ts}.
// API: README.md · Event shape: EVENTS.md · Schema: schemas/event.schema.json.

const DEFAULTS = {
  theme: 'obs',
  windowMs: 30_000,
  visibleRows: 3,
  maxEntries: 512,
  maxTextLength: 180,
  showStatus: true,
  dedupe: true,
  redact: true,
  renderScheduler: 'raf',
  sort: false,
  maxSeenIds: 2_048,
  maxFrameBytes: 256_000,
  maxFrameEvents: 10_000,
  maxPendingEvents: 10_000,
  ingestBatchSize: 500,
  batchScheduler: 'timeout',
};

const DEFAULT_RECONNECT = {
  enabled: true,
  retries: Infinity,
  baseMs: 250,
  maxMs: 8_000,
  jitterMs: 250,
};

const VALID_THEMES = new Set(['dark', 'light', 'obs']);
const VALID_DECISIONS = new Set(['pass', 'drop', 'abstain', 'override', 'idle']);
const VALID_SCHEDULERS = new Set(['raf', 'microtask', 'timeout', 'sync']);
const VALID_BATCH_SCHEDULERS = new Set(['microtask', 'timeout', 'sync']);

const FIELD_PATHS = {
  id: ['id', 'eventId', 'payload.id', 'payload.eventId', 'payload.decision.id'],
  traceId: ['traceId', 'trace_id', 'payload.traceId', 'payload.trace_id', 'payload.decision.traceId'],
  spanId: ['spanId', 'span_id', 'payload.spanId', 'payload.span_id', 'payload.decision.spanId'],
  ts: ['ts', 'timestamp', 'createdAt', 'created_at', 'time', 'payload.ts', 'payload.timestamp', 'payload.decision.ts'],
  decision: ['decision', 'payload.decision.decision', 'payload.decision', 'payload.result.decision'],
  rule: ['ruleId', 'rule', 'rule_id', 'payload.decision.ruleId', 'payload.ruleId', 'payload.rule_id'],
  command: ['command_preview', 'commandPreview', 'command', 'payload.decision.command_preview', 'payload.decision.commandPreview', 'payload.decision.command', 'payload.command'],
  reason: ['reason', 'payload.decision.reason', 'payload.reason', 'explanation'],
  confidence: ['confidence', 'score', 'payload.decision.confidence', 'payload.score'],
  latencyMs: ['latencyMs', 'latency_ms', 'durationMs', 'duration_ms', 'payload.decision.latencyMs', 'payload.latency_ms'],
  source: ['source', 'adapter', 'provider', 'payload.source', 'payload.decision.source'],
};

const EMPTY_HTML =
  '<div class="ol-row ol-row-empty">' +
  '<span class="ol-pill ol-pill-idle" role="status">audit log</span>' +
  '<span class="ol-text-dim">waiting for first decision&hellip;</span>' +
  '</div>';

function now() {
  return Date.now();
}

function perfNow() {
  return globalThis.performance?.now ? globalThis.performance.now() : Date.now();
}

function byteLength(text) {
  const value = String(text ?? '');
  if (typeof globalThis.TextEncoder === 'function') return new TextEncoder().encode(value).length;
  return value.length;
}

function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function toPositiveInt(value, fallback, max = 1_000_000) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.min(Math.floor(n), max);
}

function toNonNegativeInt(value, fallback, max = 1_000_000) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return fallback;
  return Math.min(Math.floor(n), max);
}

function toBoolean(value, fallback) {
  return typeof value === 'boolean' ? value : fallback;
}

function pick(entry, field) {
  for (const path of FIELD_PATHS[field] ?? []) {
    const value = path.split('.').reduce((node, key) => node?.[key], entry);
    if (value != null && value !== '') return value;
  }
  return undefined;
}

function parseTsMs(raw) {
  if (raw == null || raw === '') return null;
  if (typeof raw === 'number') {
    if (!Number.isFinite(raw)) return null;
    return raw < 10_000_000_000 ? Math.floor(raw * 1000) : Math.floor(raw);
  }
  const numeric = Number(raw);
  if (Number.isFinite(numeric) && /^\d+(\.\d+)?$/.test(String(raw))) {
    return numeric < 10_000_000_000 ? Math.floor(numeric * 1000) : Math.floor(numeric);
  }
  const parsed = new Date(String(raw)).getTime();
  return Number.isNaN(parsed) ? null : parsed;
}

function formatClock(tsMs) {
  if (tsMs == null) return '';
  const d = new Date(tsMs);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, '0'))
    .join(':');
}

function normalizeDecision(value) {
  const raw = typeof value === 'object' && value ? value.decision : value;
  const decision = String(raw ?? 'idle').trim().toLowerCase();
  return VALID_DECISIONS.has(decision) ? decision : 'idle';
}

function normalizeNumber(value, { min = -Infinity, max = Infinity } = {}) {
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  return Math.min(Math.max(n, min), max);
}

function clampText(value, maxTextLength) {
  const text = String(value ?? '');
  if (text.length <= maxTextLength) return text;
  return `${text.slice(0, Math.max(0, maxTextLength - 1))}…`;
}

function defaultRedactor(input) {
  let text = String(input ?? '');
  text = text.replace(/\b(Bearer|Token|Basic)\s+[A-Za-z0-9._~+/=-]{8,}/gi, '$1 [redacted]');
  text = text.replace(/\bsk-[A-Za-z0-9_-]{12,}\b/g, 'sk-[redacted]');
  text = text.replace(/\b(AKIA|ASIA)[A-Z0-9]{12,}\b/g, '[aws-key-redacted]');
  text = text.replace(/\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/g, '[jwt-redacted]');
  text = text.replace(/\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*)=([^\s;&]+)/gi, '$1=[redacted]');
  text = text.replace(/([?&](?:token|access_token|api_key|apikey|key|secret|password|sig|signature)=)[^&\s]+/gi, '$1[redacted]');
  return text;
}

function makeRedactor(redact) {
  if (redact === false) return (value) => String(value ?? '');
  if (typeof redact === 'function') return (value) => String(redact(String(value ?? '')) ?? '');
  return defaultRedactor;
}

function resolveContainer(container) {
  if (typeof container === 'string') {
    return typeof document === 'undefined' ? null : document.querySelector(container);
  }
  return container ?? null;
}

function buildConfig(opts) {
  const reconnectInput = opts.reconnect === false ? { enabled: false } : (opts.reconnect ?? {});
  const reconnect = { ...DEFAULT_RECONNECT, ...reconnectInput };
  reconnect.enabled = opts.reconnect === false ? false : toBoolean(reconnect.enabled, DEFAULT_RECONNECT.enabled);
  reconnect.retries = reconnect.retries === Infinity
    ? Infinity
    : toNonNegativeInt(reconnect.retries, DEFAULT_RECONNECT.retries, 100_000);
  reconnect.baseMs = toPositiveInt(reconnect.baseMs, DEFAULT_RECONNECT.baseMs, 60_000);
  reconnect.maxMs = Math.max(reconnect.baseMs, toPositiveInt(reconnect.maxMs, DEFAULT_RECONNECT.maxMs, 300_000));
  reconnect.jitterMs = toNonNegativeInt(reconnect.jitterMs, DEFAULT_RECONNECT.jitterMs, 60_000);

  const scheduler = VALID_SCHEDULERS.has(opts.renderScheduler) ? opts.renderScheduler : DEFAULTS.renderScheduler;
  const batchScheduler = VALID_BATCH_SCHEDULERS.has(opts.batchScheduler) ? opts.batchScheduler : DEFAULTS.batchScheduler;
  const decisions = opts.decisions ? new Set([...opts.decisions].map((v) => normalizeDecision(v))) : null;

  return {
    theme: VALID_THEMES.has(opts.theme) ? opts.theme : DEFAULTS.theme,
    windowMs: toPositiveInt(opts.windowMs, DEFAULTS.windowMs, 24 * 60 * 60 * 1000),
    visibleRows: toPositiveInt(opts.visibleRows, DEFAULTS.visibleRows, 100),
    maxEntries: toPositiveInt(opts.maxEntries, DEFAULTS.maxEntries, 100_000),
    maxTextLength: toPositiveInt(opts.maxTextLength, DEFAULTS.maxTextLength, 10_000),
    showStatus: toBoolean(opts.showStatus, DEFAULTS.showStatus),
    dedupe: toBoolean(opts.dedupe, DEFAULTS.dedupe),
    sort: toBoolean(opts.sort, DEFAULTS.sort),
    maxSeenIds: toPositiveInt(opts.maxSeenIds, DEFAULTS.maxSeenIds, 100_000),
    maxFrameBytes: toPositiveInt(opts.maxFrameBytes, DEFAULTS.maxFrameBytes, 10_000_000),
    maxFrameEvents: toPositiveInt(opts.maxFrameEvents, DEFAULTS.maxFrameEvents, 1_000_000),
    maxPendingEvents: toPositiveInt(opts.maxPendingEvents, DEFAULTS.maxPendingEvents, 1_000_000),
    ingestBatchSize: toPositiveInt(opts.ingestBatchSize, DEFAULTS.ingestBatchSize, 100_000),
    batchScheduler,
    redactor: makeRedactor(opts.redact ?? DEFAULTS.redact),
    renderScheduler: scheduler,
    reconnect,
    decisions,
    minConfidence: opts.minConfidence == null ? null : normalizeNumber(opts.minConfidence, { min: 0, max: 1 }),
    filter: typeof opts.filter === 'function' ? opts.filter : null,
    onEvent: typeof opts.onEvent === 'function' ? opts.onEvent : null,
    onStatus: typeof opts.onStatus === 'function' ? opts.onStatus : null,
    onRender: typeof opts.onRender === 'function' ? opts.onRender : null,
  };
}

class RingLog {
  constructor(limit) {
    this.limit = limit;
    this.items = new Array(limit);
    this.start = 0;
    this.size = 0;
  }

  push(item) {
    let dropped = null;
    if (this.size < this.limit) {
      this.items[(this.start + this.size) % this.limit] = item;
      this.size += 1;
    } else {
      dropped = this.items[this.start] ?? null;
      this.items[this.start] = item;
      this.start = (this.start + 1) % this.limit;
    }
    return dropped;
  }

  clear() {
    this.items = new Array(this.limit);
    this.start = 0;
    this.size = 0;
  }

  toArray() {
    const out = new Array(this.size);
    for (let i = 0; i < this.size; i += 1) out[i] = this.items[(this.start + i) % this.limit];
    return out;
  }

  replace(items) {
    this.clear();
    for (const item of items.slice(-this.limit)) this.push(item);
  }
}

function createStats() {
  return {
    frames: 0,
    acceptedEvents: 0,
    ignoredEvents: 0,
    malformedFrames: 0,
    oversizedFrames: 0,
    frameEventsDropped: 0,
    dedupedEvents: 0,
    filterRejectedEvents: 0,
    trimmedEntries: 0,
    droppedEntries: 0,
    backpressureDroppedEvents: 0,
    queuedEvents: 0,
    pendingEvents: 0,
    maxPendingEventsSeen: 0,
    processedBatches: 0,
    reconnects: 0,
    renderCount: 0,
    scheduledRenderCount: 0,
    lastFrameAt: null,
    lastEventAt: null,
    lastRenderAt: null,
    lastRenderDurationMs: 0,
    maxRenderDurationMs: 0,
    avgRenderDurationMs: 0,
    lastError: '',
    decisionCounts: { pass: 0, drop: 0, abstain: 0, override: 0, idle: 0 },
  };
}

function cloneStats(stats, startedAt) {
  const uptimeSec = Math.max(0.001, (now() - startedAt) / 1000);
  return {
    ...stats,
    decisionCounts: { ...stats.decisionCounts },
    eventsPerSecond: stats.acceptedEvents / uptimeSec,
  };
}

function statusHtml(status, stats) {
  const text = status.paused ? 'paused' : status.state;
  const detail = status.message || status.transport || 'manual';
  const suffix = status.reconnectAttempt ? ` · retry ${status.reconnectAttempt}` : '';
  return [
    `<div class="ol-row ol-row-status" role="status" data-status="${escapeHtml(text)}">`,
    `<span class="ol-dot ol-dot-${escapeHtml(text)}"></span>`,
    `<span class="ol-status-label">${escapeHtml(text)}</span>`,
    `<span class="ol-text-dim">${escapeHtml(detail)}${escapeHtml(suffix)}</span>`,
    `<span class="ol-ts">${stats.acceptedEvents ? escapeHtml(String(stats.acceptedEvents)) : ''}</span>`,
    '</div>',
  ].join('');
}

function rowHtml(entry, isNew) {
  const safe = VALID_DECISIONS.has(entry.decision) ? entry.decision : 'idle';
  const meta = [];
  if (entry.confidence != null) meta.push(`${Math.round(entry.confidence * 100)}%`);
  if (entry.latencyMs != null) meta.push(`${Math.round(entry.latencyMs)}ms`);
  if (entry.source) meta.push(entry.source);
  if (entry.traceId) meta.push(`trace ${entry.traceId}`);

  return [
    `<div class="ol-row${isNew ? ' ol-row-new' : ''}" role="listitem" data-decision="${safe}" data-seq="${entry.sequence}">`,
    `<span class="ol-pill ol-pill-${safe}" role="status" aria-label="decision ${safe}">${safe}</span>`,
    `<span class="ol-cmd ol-cmd-${safe}">`,
    entry.rule ? `<span class="ol-rule ol-rule-${safe}">${escapeHtml(entry.rule)}</span> ` : '',
    escapeHtml(entry.command),
    entry.reason ? `<span class="ol-reason">${escapeHtml(entry.reason)}</span>` : '',
    meta.length ? `<span class="ol-meta">${escapeHtml(meta.join(' · '))}</span>` : '',
    '</span>',
    entry.clock ? `<span class="ol-ts">${escapeHtml(entry.clock)}</span>` : '<span></span>',
    '</div>',
  ].join('');
}

export function mount(opts = {}) {
  const root = resolveContainer(opts.container ?? '#root');
  if (!root) throw new Error('SaaOverlay.mount: container not found');

  const config = buildConfig(opts);
  const startedAt = now();
  const log = new RingLog(config.maxEntries);
  const stats = createStats();
  const subscribers = new Set();
  const seenIds = new Set();
  const seenQueue = [];

  let sequence = 0;
  let newestTsMs = 0;
  let lastRenderedSequence = 0;
  let disposed = false;
  let paused = false;
  let closeTransport = null;
  let reconnectTimer = null;
  let reconnectAttempt = 0;
  let renderScheduled = false;
  let renderGeneration = 0;
  let status = {
    state: opts.source ? 'connecting' : 'idle',
    transport: opts.source ? 'stream' : 'manual',
    message: opts.source ? 'waiting for events' : 'push() ready',
    reconnectAttempt: 0,
    paused: false,
  };

  const pending = [];
  const drainResolvers = [];
  let pendingHead = 0;
  let processScheduled = false;

  if (root.ownerDocument?.body) root.ownerDocument.body.setAttribute('data-theme', config.theme);
  root.setAttribute('role', 'log');
  root.setAttribute('aria-live', 'polite');
  if (!root.getAttribute('aria-label')) root.setAttribute('aria-label', 'SAA decision flight recorder');

  function notify(type, payload = {}) {
    const event = { type, ...payload };
    for (const fn of subscribers) {
      try { fn(event); } catch { /* subscriber isolation */ }
    }
    if (type === 'entry') {
      try { config.onEvent?.(payload.entry, cloneStats(stats, startedAt)); } catch { /* callback isolation */ }
    } else if (type === 'status') {
      try { config.onStatus?.({ ...status }); } catch { /* callback isolation */ }
    } else if (type === 'render') {
      try { config.onRender?.(payload); } catch { /* callback isolation */ }
    }
  }

  function setStatus(nextState, patch = {}) {
    status = {
      ...status,
      ...patch,
      state: nextState,
      paused,
      reconnectAttempt,
    };
    notify('status', { status: { ...status } });
    scheduleRender();
  }

  function visibleEntries() {
    let entries = log.toArray();
    if (config.sort) {
      entries = entries.slice().sort((a, b) => (a.tsMs ?? 0) - (b.tsMs ?? 0) || a.sequence - b.sequence);
    }
    const visible = [];
    for (const entry of entries) {
      if (entry.renderVisible) visible.push(entry);
    }
    return visible.slice(-config.visibleRows);
  }

  function trimWindow() {
    if (!newestTsMs) return;
    const before = log.size;
    const live = log.toArray().filter((entry) => entry.tsMs == null || newestTsMs - entry.tsMs <= config.windowMs);
    if (live.length !== before) {
      stats.trimmedEntries += before - live.length;
      log.replace(live);
    }
  }

  function renderNow() {
    if (disposed) return;
    const started = perfNow();
    renderScheduled = false;
    renderGeneration += 1;
    trimWindow();

    const rows = visibleEntries();
    const newestSeq = rows.length ? rows[rows.length - 1].sequence : 0;
    const fresh = lastRenderedSequence && newestSeq !== lastRenderedSequence;
    lastRenderedSequence = newestSeq || lastRenderedSequence;

    const body = rows.length
      ? rows.map((entry, idx, arr) => rowHtml(entry, fresh && idx === arr.length - 1)).join('')
      : EMPTY_HTML;
    root.innerHTML = (config.showStatus ? statusHtml(status, stats) : '') + body;

    const duration = Math.max(0, perfNow() - started);
    stats.renderCount += 1;
    stats.lastRenderAt = now();
    stats.lastRenderDurationMs = duration;
    stats.maxRenderDurationMs = Math.max(stats.maxRenderDurationMs, duration);
    stats.avgRenderDurationMs = stats.avgRenderDurationMs + ((duration - stats.avgRenderDurationMs) / stats.renderCount);
    notify('render', { durationMs: duration, visibleRows: rows.length, stats: cloneStats(stats, startedAt) });
  }

  function scheduleRender() {
    if (disposed || paused) return;
    if (config.renderScheduler === 'sync') {
      renderNow();
      return;
    }
    if (renderScheduled) return;
    renderScheduled = true;
    stats.scheduledRenderCount += 1;
    const generation = ++renderGeneration;
    const run = () => {
      if (!renderScheduled || generation !== renderGeneration || disposed || paused) return;
      renderNow();
    };
    if (config.renderScheduler === 'raf' && typeof globalThis.requestAnimationFrame === 'function') {
      globalThis.requestAnimationFrame(run);
    } else if ((config.renderScheduler === 'raf' || config.renderScheduler === 'microtask') && typeof globalThis.queueMicrotask === 'function') {
      globalThis.queueMicrotask(run);
    } else {
      setTimeout(run, 0);
    }
  }

  function rememberId(id) {
    if (!id) return false;
    if (seenIds.has(id)) return true;
    seenIds.add(id);
    seenQueue.push(id);
    while (seenQueue.length > config.maxSeenIds) {
      const old = seenQueue.shift();
      seenIds.delete(old);
    }
    return false;
  }

  function pendingDepth() {
    return pending.length - pendingHead;
  }

  function updateQueueStats() {
    const depth = pendingDepth();
    stats.pendingEvents = depth;
    stats.maxPendingEventsSeen = Math.max(stats.maxPendingEventsSeen, depth);
  }

  function compactPending() {
    if (pendingHead > 1024 && pendingHead * 2 > pending.length) {
      pending.splice(0, pendingHead);
      pendingHead = 0;
    }
  }

  function resolveDrainIfIdle() {
    if (processScheduled || pendingDepth() > 0) return;
    while (drainResolvers.length) {
      const resolve = drainResolvers.shift();
      try { resolve(); } catch { /* noop */ }
    }
  }

  function dropPending(count) {
    const dropped = Math.min(Math.max(0, count), pendingDepth());
    if (!dropped) return 0;
    pendingHead += dropped;
    stats.backpressureDroppedEvents += dropped;
    compactPending();
    updateQueueStats();
    return dropped;
  }

  function processOneBatch() {
    let processed = 0;
    while (!disposed && processed < config.ingestBatchSize && pendingHead < pending.length) {
      accept(pending[pendingHead]);
      pendingHead += 1;
      processed += 1;
    }
    if (processed) {
      stats.processedBatches += 1;
      updateQueueStats();
      compactPending();
      scheduleRender();
    }
    return processed;
  }

  function scheduleProcess() {
    if (disposed) return;
    if (processScheduled) return;

    if (config.batchScheduler === 'sync') {
      while (pendingDepth() > 0 && !disposed) processOneBatch();
      updateQueueStats();
      resolveDrainIfIdle();
      return;
    }

    processScheduled = true;
    const run = () => {
      if (disposed) return;
      processScheduled = false;
      processOneBatch();
      if (pendingDepth() > 0) scheduleProcess();
      else resolveDrainIfIdle();
    };

    if (config.batchScheduler === 'microtask' && typeof globalThis.queueMicrotask === 'function') {
      globalThis.queueMicrotask(run);
    } else {
      setTimeout(run, 0);
    }
  }

  function enqueue(items) {
    if (!items.length || disposed) return;
    let incoming = items;
    if (incoming.length > config.maxPendingEvents) {
      stats.backpressureDroppedEvents += incoming.length - config.maxPendingEvents;
      incoming = incoming.slice(-config.maxPendingEvents);
    }

    const overflow = pendingDepth() + incoming.length - config.maxPendingEvents;
    if (overflow > 0) dropPending(overflow);

    for (const item of incoming) pending.push(item);
    stats.queuedEvents += incoming.length;
    updateQueueStats();
    scheduleProcess();
  }

  function normalize(raw) {
    if (!raw || typeof raw !== 'object') return null;
    const rawTs = pick(raw, 'ts');
    const tsMs = parseTsMs(rawTs);
    if (tsMs != null) newestTsMs = Math.max(newestTsMs, tsMs);

    const decision = normalizeDecision(pick(raw, 'decision'));
    const confidence = normalizeNumber(pick(raw, 'confidence'), { min: 0, max: 1 });
    const latencyMs = normalizeNumber(pick(raw, 'latencyMs'), { min: 0 });
    const traceId = clampText(config.redactor(pick(raw, 'traceId') ?? ''), config.maxTextLength);
    const spanId = clampText(config.redactor(pick(raw, 'spanId') ?? ''), config.maxTextLength);
    const id = clampText(config.redactor(pick(raw, 'id') ?? (traceId && spanId ? `${traceId}:${spanId}:${decision}` : '')), config.maxTextLength);

    const entry = {
      id,
      sequence: ++sequence,
      ts: rawTs == null ? '' : String(rawTs),
      tsMs,
      clock: formatClock(tsMs),
      decision,
      rule: clampText(config.redactor(pick(raw, 'rule') ?? ''), config.maxTextLength),
      command: clampText(config.redactor(pick(raw, 'command') ?? ''), config.maxTextLength),
      reason: clampText(config.redactor(pick(raw, 'reason') ?? ''), config.maxTextLength),
      traceId,
      spanId,
      confidence,
      latencyMs,
      source: clampText(config.redactor(pick(raw, 'source') ?? ''), config.maxTextLength),
      renderVisible: true,
    };

    if (config.decisions && !config.decisions.has(entry.decision)) entry.renderVisible = false;
    if (config.minConfidence != null && (entry.confidence == null || entry.confidence < config.minConfidence)) entry.renderVisible = false;
    if (config.filter) {
      try {
        if (!config.filter(entry, raw)) entry.renderVisible = false;
      } catch (err) {
        entry.renderVisible = false;
        stats.lastError = `filter error: ${err?.message ?? String(err)}`;
      }
    }
    return entry;
  }

  function accept(raw) {
    const entry = normalize(raw);
    if (!entry) {
      stats.ignoredEvents += 1;
      return;
    }
    if (config.dedupe && entry.id && rememberId(entry.id)) {
      stats.dedupedEvents += 1;
      return;
    }
    if (!entry.renderVisible) stats.filterRejectedEvents += 1;
    const dropped = log.push(entry);
    if (dropped) stats.droppedEntries += 1;
    stats.acceptedEvents += 1;
    stats.lastEventAt = now();
    stats.decisionCounts[entry.decision] += 1;
    notify('entry', { entry });
  }

  function ingest(payload) {
    if (disposed) return;
    let items = Array.isArray(payload) ? payload : [payload];
    if (items.length > config.maxFrameEvents) {
      stats.frameEventsDropped += items.length - config.maxFrameEvents;
      items = items.slice(-config.maxFrameEvents);
    }

    if (items.length > config.ingestBatchSize) {
      enqueue(items);
      return;
    }

    for (const item of items) accept(item);
    scheduleRender();
    resolveDrainIfIdle();
  }

  function onFrame(message) {
    if (disposed) return;
    const data = message?.data ?? message;
    stats.frames += 1;
    stats.lastFrameAt = now();
    if (typeof data === 'string') {
      if (byteLength(data) > config.maxFrameBytes) {
        stats.oversizedFrames += 1;
        stats.lastError = `oversized frame: ${byteLength(data)} bytes > ${config.maxFrameBytes}`;
        scheduleRender();
        return;
      }
      try {
        ingest(JSON.parse(data));
      } catch (err) {
        stats.malformedFrames += 1;
        stats.lastError = `malformed frame: ${err?.message ?? String(err)}`;
        scheduleRender();
      }
    } else if (data && typeof data === 'object') {
      ingest(data);
    } else {
      stats.ignoredEvents += 1;
      scheduleRender();
    }
  }

  function clearReconnectTimer() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  function attachEventSource(es, owned = false) {
    status.transport = 'eventsource';
    setStatus('connecting', { transport: 'eventsource', message: owned ? 'opening SSE' : 'attached SSE' });
    es.onopen = () => { reconnectAttempt = 0; setStatus('connected', { transport: 'eventsource', message: 'SSE connected' }); };
    es.onmessage = onFrame;
    es.onerror = () => {
      stats.lastError = 'eventsource error';
      setStatus('error', { transport: 'eventsource', message: 'SSE error / auto-retry' });
    };
    closeTransport = () => { try { es.close(); } catch { /* noop */ } };
    if (!owned) setStatus('connected', { transport: 'eventsource', message: 'attached SSE' });
  }

  function attachExternalWebSocket(ws) {
    status.transport = 'websocket';
    setStatus('connected', { transport: 'websocket', message: 'attached WebSocket' });
    ws.onopen = () => setStatus('connected', { transport: 'websocket', message: 'WebSocket open' });
    ws.onmessage = onFrame;
    ws.onerror = () => { stats.lastError = 'websocket error'; setStatus('error', { transport: 'websocket', message: 'WebSocket error' }); };
    ws.onclose = () => setStatus('closed', { transport: 'websocket', message: 'WebSocket closed' });
    closeTransport = () => { try { ws.close(); } catch { /* noop */ } };
  }

  function connectWebSocketUrl(url) {
    if (disposed || typeof WebSocket === 'undefined') return;
    clearReconnectTimer();
    setStatus(reconnectAttempt ? 'reconnecting' : 'connecting', { transport: 'websocket', message: url });
    const ws = new WebSocket(url);
    closeTransport = () => { try { ws.close(); } catch { /* noop */ } };
    ws.onopen = () => {
      reconnectAttempt = 0;
      setStatus('connected', { transport: 'websocket', message: url });
    };
    ws.onmessage = onFrame;
    ws.onerror = () => {
      stats.lastError = 'websocket error';
      setStatus('error', { transport: 'websocket', message: url });
    };
    ws.onclose = () => {
      if (disposed) return;
      const { reconnect } = config;
      if (!reconnect.enabled || reconnectAttempt >= reconnect.retries) {
        setStatus('closed', { transport: 'websocket', message: url });
        return;
      }
      reconnectAttempt += 1;
      stats.reconnects += 1;
      const exp = Math.min(reconnect.maxMs, reconnect.baseMs * (2 ** Math.max(0, reconnectAttempt - 1)));
      const jitter = reconnect.jitterMs ? Math.floor(Math.random() * reconnect.jitterMs) : 0;
      const delay = exp + jitter;
      setStatus('reconnecting', { transport: 'websocket', message: `${url} in ${delay}ms` });
      reconnectTimer = setTimeout(() => connectWebSocketUrl(url), delay);
    };
  }

  function attachSource(source) {
    if (!source) return;
    if (typeof source === 'string') {
      if (/^wss?:\/\//i.test(source)) {
        connectWebSocketUrl(source);
      } else if (typeof EventSource !== 'undefined') {
        attachEventSource(new EventSource(source), true);
      } else {
        stats.lastError = 'EventSource unavailable';
        setStatus('error', { transport: 'eventsource', message: 'EventSource unavailable' });
      }
    } else if (typeof source === 'object') {
      if (typeof source.send === 'function') attachExternalWebSocket(source);
      else attachEventSource(source, false);
    }
  }

  attachSource(opts.source);
  renderNow();

  const handle = {
    unmount() {
      disposed = true;
      clearReconnectTimer();
      if (closeTransport) closeTransport();
      closeTransport = null;
      log.clear();
      seenIds.clear();
      seenQueue.length = 0;
      pending.length = 0;
      pendingHead = 0;
      processScheduled = false;
      updateQueueStats();
      resolveDrainIfIdle();
      root.innerHTML = '';
      root.removeAttribute('role');
      root.removeAttribute('aria-live');
    },
    push: ingest,
    clear() {
      log.clear();
      seenIds.clear();
      seenQueue.length = 0;
      pending.length = 0;
      pendingHead = 0;
      processScheduled = false;
      updateQueueStats();
      resolveDrainIfIdle();
      newestTsMs = 0;
      lastRenderedSequence = 0;
      scheduleRender();
    },
    pause() {
      paused = true;
      status = { ...status, paused: true, state: 'paused' };
      notify('status', { status: { ...status } });
      renderNow();
    },
    resume() {
      paused = false;
      const next = closeTransport ? 'connected' : 'idle';
      setStatus(next, { message: closeTransport ? status.message : 'push() ready' });
      scheduleRender();
    },
    flush() {
      processScheduled = false;
      while (pendingDepth() > 0 && !disposed) processOneBatch();
      updateQueueStats();
      resolveDrainIfIdle();
      renderNow();
    },
    drain() {
      if (disposed || (!processScheduled && pendingDepth() === 0)) return Promise.resolve();
      return new Promise((resolve) => { drainResolvers.push(resolve); });
    },
    subscribe(fn) {
      if (typeof fn !== 'function') return () => {};
      subscribers.add(fn);
      return () => subscribers.delete(fn);
    },
    setFilter(filter) {
      config.filter = typeof filter === 'function' ? filter : null;
      for (const entry of log.toArray()) {
        entry.renderVisible = true;
        if (config.decisions && !config.decisions.has(entry.decision)) entry.renderVisible = false;
        if (config.minConfidence != null && (entry.confidence == null || entry.confidence < config.minConfidence)) entry.renderVisible = false;
        if (config.filter) {
          try { if (!config.filter(entry, null)) entry.renderVisible = false; } catch { entry.renderVisible = false; }
        }
      }
      scheduleRender();
    },
    setTheme(theme) {
      if (!VALID_THEMES.has(theme)) return;
      config.theme = theme;
      if (root.ownerDocument?.body) root.ownerDocument.body.setAttribute('data-theme', theme);
    },
    getEntries: () => log.toArray().slice(),
    getVisibleEntries: () => visibleEntries(),
    getStats: () => cloneStats(stats, startedAt),
    getStatus: () => ({ ...status }),
    getHealth() {
      const staleMs = stats.lastFrameAt == null ? null : now() - stats.lastFrameAt;
      const queueDepth = pendingDepth();
      const transportHealthy = status.state === 'idle' || status.state === 'connected' || status.state === 'paused';
      return {
        ok: transportHealthy && queueDepth < config.maxPendingEvents && !stats.lastError,
        state: status.state,
        transport: status.transport,
        staleMs,
        pendingEvents: queueDepth,
        acceptedEvents: stats.acceptedEvents,
        lastError: stats.lastError,
      };
    },
    toJSON: () => ({ entries: log.toArray(), stats: cloneStats(stats, startedAt), status: { ...status }, health: handle.getHealth() }),
    exportJSON: () => JSON.stringify(handle.toJSON()),
  };

  return handle;
}

export const SaaOverlay = { mount };
export default SaaOverlay;

// Browser global side-effect: when loaded in a browser via
// `<script type="module" src="overlay.js">`, also attach to window so a
// non-module inline <script> can call SaaOverlay.mount() without re-importing.
if (typeof globalThis !== 'undefined' && globalThis.window === globalThis) {
  globalThis.SaaOverlay = SaaOverlay;
}
