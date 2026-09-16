// warmup progress for the prediction card
// `started` = model loaded; its first real prediction needs a full sequence
// buffer (DEFAULT_TICKS rows at 250 ms), announced by `warmup_complete`.
// Buffer-fill predictions carry confidence 0, so a confident tick is the
// fallback signal (same rule as the streaming SDKs)
export const DEFAULT_TICKS = 50;

export function createWarmupTracker(totalTicks = DEFAULT_TICKS) {
  let phase = "idle"; // idle | loading | filling | done
  let ticks = 0;
  return {
    get phase() { return phase; },
    reset() { phase = "idle"; ticks = 0; },
    connect() { phase = "loading"; ticks = 0; },
    started() { if (phase !== "done") phase = "filling"; },
    complete() { phase = "done"; },
    // one prediction envelope -> { fill: 0..1, done }
    prediction(p) {
      if (phase === "done") return { fill: 1, done: true };
      if ((p.confidence ?? 0) > 0) { phase = "done"; return { fill: 1, done: true }; }
      phase = "filling";
      ticks += 1;
      // servers may report buffer_size / sequence_length; otherwise count ticks
      const fill = p.sequence_length > 0 && p.buffer_size != null
        ? p.buffer_size / p.sequence_length
        : ticks / totalTicks;
      // hold just short of full until the server confirms
      return { fill: Math.min(fill, 0.98), done: false };
    },
  };
}
