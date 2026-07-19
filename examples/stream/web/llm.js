// OpenAI Realtime bridge — sample-app only, NOT part of the SDK.
// The SDK emits `turnReady` with base64 PCM16 + optional JPEG frames;
// this helper wraps them into OpenAI's input_audio / input_image content
// parts and plays the audio response back through WebAudio.

const REALTIME_BASE = "wss://api.openai.com/v1/realtime";
// TTFB probe: gpt-realtime-mini is a smaller, NON-reasoning realtime model, so
// it has lower time-to-first-audio than gpt-realtime-2 (at some quality cost).
// To trade back for quality, set DEFAULT_MODEL = "gpt-realtime-2" and
// DEFAULT_REASONING_EFFORT = "minimal" (mini has no reasoning, so the reasoning
// field is omitted whenever the effort is null).
const DEFAULT_MODEL = "gpt-realtime-2";
const DEFAULT_REASONING_EFFORT = null; // null | "minimal" | "low" | "medium" | "high" | "xhigh"
const DEFAULT_VOICE = "sage";
const OUTPUT_SAMPLE_RATE = 24000;
const DEFAULT_GAIN_DB = 6;
// Option A — stream each audio delta as it arrives (gapless) instead of
// buffering until response.done. Flip to false for the buffered fallback.
const STREAMING_PLAYBACK = true;
const STREAM_LEAD_IN = 0.06; // s of scheduling headroom before the first chunk

export class RealtimeLLMBridge {
  constructor(options) {
    if (!options?.apiKey) throw new Error("RealtimeLLMBridge: apiKey required");
    this.apiKey = options.apiKey;
    this.model = options.model ?? DEFAULT_MODEL;
    // Reasoning effort only applies to reasoning-capable models (gpt-realtime-2);
    // null omits the field, which is required for non-reasoning models like mini.
    this.reasoningEffort = options.reasoningEffort ?? DEFAULT_REASONING_EFFORT;
    this.url = options.url ?? `${REALTIME_BASE}?model=${this.model}`;
    this.voice = options.voice ?? DEFAULT_VOICE;
    this.instructions = options.instructions ?? "You are a helpful assistant.";
    this.gainDb = options.gainDb ?? DEFAULT_GAIN_DB;
    this.temperature = options.temperature ?? 0.8;

    this.ws = null;
    this.sessionReady = false;
    this.pendingAudio = null;
    this.pendingFrames = [];
    this.pendingGreeting = null;
    this.audioChunks = [];
    this.responseTimer = null;
    this.closed = false;
    this.listeners = new Map();
    // Held during _playback so interrupt() can fade the gain to 0 and stop
    // the source. Cleared in src.onended.
    this._activeCtx = null;
    this._activeSrc = null;
    this._activeGain = null;
    // True between interrupt() and the next sendAudioB64/greet. Causes the
    // next _playback to skip — needed because OpenAI may finish flushing
    // the in-flight response after we've sent response.cancel, and we don't
    // want that stale audio to start playing right after we faded.
    this._suppressNextPlayback = false;
    // OpenAI's response id, set on response.created and cleared on response.done.
    // Used by interrupt() to decide whether to send response.cancel — sending
    // it once response.done has landed yields the response_cancel_not_active
    // 400 error from the API and serves no purpose.
    this._activeResponseId = null;
    // P0 latency instrumentation: perf-clock stamps across one response.
    // responseTimer (send) is set in _flush/_flushGreeting; these mark the
    // LLM ack and first audio token so _logTurnTiming can break down the turn.
    this._responseCreatedAt = null;
    this._firstAudioAt = null;
    // P0 context-loss: true once a socket has opened, so a later _connect is
    // recognized as a reconnect (OpenAI server-side history reset).
    this._hasConnected = false;
    // Option A streaming playback: persistent AudioContext + active stream
    // record (keyed by responseId). null when nothing is streaming.
    this._playCtx = null;
    this._stream = null;
  }

  on(event, fn) {
    let set = this.listeners.get(event);
    if (!set) {
      set = new Set();
      this.listeners.set(event, set);
    }
    set.add(fn);
    return () => set.delete(fn);
  }

  _emit(event, payload) {
    const set = this.listeners.get(event);
    if (!set) return;
    for (const fn of set) {
      try {
        fn(payload);
      } catch (err) {
        console.error(`[llm] listener '${event}' threw:`, err);
      }
    }
  }

  sendAudioB64(b64, frames = []) {
    this.pendingAudio = b64;
    this.pendingFrames = Array.isArray(frames) ? frames : [];
    this.closed = false;
    // New user turn — clear any interrupt-suppression so the next response plays.
    this._suppressNextPlayback = false;
    if (this.sessionReady && this.ws?.readyState === WebSocket.OPEN) {
      this._flush();
      return;
    }
    this._connect();
  }

  // Trigger an audio response from text instructions alone (no user audio).
  // Useful for proactive greetings after warmup.
  greet(instructions) {
    this.pendingGreeting = instructions;
    this.closed = false;
    if (this.sessionReady && this.ws?.readyState === WebSocket.OPEN) {
      this._flushGreeting();
      return;
    }
    this._connect();
  }

  // Open the WS + run session.update without sending any audio or response
  // request. Lets the page mask the ~300-800ms WS handshake + ~100ms
  // session.update behind the SAS warmup window so the first greet/send is
  // near-instant. Safe to call repeatedly — no-ops if already connecting.
  prewarm() {
    this.closed = false;
    this._connect();
  }

  _connect() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    // A second connect means the prior LLM socket dropped — OpenAI's
    // server-side conversation history is gone, so the next turn starts fresh.
    // Flag it so context loss is visible, not silent.
    if (this._hasConnected) {
      console.warn("[llm] opening new LLM session — conversation context will reset (prior socket dropped)");
      this._emit("sessionReset", { cause: "reconnect" });
    }
    this._hasConnected = true;
    this.sessionReady = false;

    // OpenAI accepts the API key as a WS subprotocol for browser clients.
    // Note: exposes the key to the browser — only do this for local demos.
    this.ws = new WebSocket(this.url, [
      "realtime",
      `openai-insecure-api-key.${this.apiKey}`,
    ]);

    this.ws.onopen = () => {
      const session = {
        type: "realtime",
        model: this.model,
        output_modalities: ["audio"],
        instructions: this.instructions,
        audio: {
          input: {
            format: { type: "audio/pcm", rate: 24000 },
            turn_detection: null,
            transcription: { model: "whisper-1" },
          },
          output: {
            format: { type: "audio/pcm", rate: 24000 },
            voice: this.voice,
          },
        },
      };
      // Lower reasoning effort = lower TTFB, but only valid on reasoning-capable
      // models. Omitted when null (e.g. gpt-realtime-mini, which has no reasoning).
      if (this.reasoningEffort) session.reasoning = { effort: this.reasoningEffort };
      this.ws.send(JSON.stringify({ type: "session.update", session }));
    };

    this.ws.onmessage = (e) => this._onMessage(e);
    this.ws.onerror = () => {};
    this.ws.onclose = (e) => {
      this.sessionReady = false;
      this.ws = null;
      if (!this.closed) {
        console.warn(`[llm] socket closed: code=${e.code} reason=${e.reason || "none"} — conversation context will reset on next turn`);
      }
      if ((this.pendingAudio || this.pendingGreeting) && !this.closed) {
        this.pendingAudio = null;
        this.pendingFrames = [];
        this.pendingGreeting = null;
        this._emit("error", {
          title: "LLM Disconnected",
          message: "LLM connection dropped mid-request.",
          detail: `code=${e.code} reason=${e.reason || "none"}`,
        });
        this._emit("speakingEnd");
      }
    };
  }

  _flush() {
    if (!this.pendingAudio) return;
    const audio = this.pendingAudio;
    const frames = this.pendingFrames;
    this.pendingAudio = null;
    this.pendingFrames = [];
    this.responseTimer = performance.now();
    const content = [
      ...frames.map((f) => ({
        type: "input_image",
        image_url: `data:image/jpeg;base64,${f.imageBase64}`,
      })),
      { type: "input_audio", audio },
    ];
    try {
      this.ws.send(JSON.stringify({
        type: "conversation.item.create",
        item: { type: "message", role: "user", content },
      }));
      this.ws.send(JSON.stringify({ type: "response.create" }));
    } catch (err) {
      this._emit("error", {
        title: "LLM Send Error",
        message: err.message ?? String(err),
      });
      this._emit("speakingEnd");
    }
  }

  _flushGreeting() {
    if (!this.pendingGreeting) return;
    const instructions = this.pendingGreeting;
    this.pendingGreeting = null;
    this.responseTimer = performance.now();
    try {
      this.ws.send(JSON.stringify({
        type: "response.create",
        response: { instructions },
      }));
    } catch (err) {
      this._emit("error", {
        title: "LLM Greet Error",
        message: err.message ?? String(err),
      });
      this._emit("speakingEnd");
    }
  }

  _onMessage(e) {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }

    switch (data.type) {
      case "session.updated":
        if (!this.sessionReady) {
          this.sessionReady = true;
          this._flushGreeting();
          this._flush();
        }
        break;
      case "session.created":
        // Server-default config; wait for session.updated before flushing.
        break;
      case "response.created":
        // OpenAI response lifecycle starts here. A response can contain
        // multiple output_items (e.g., reasoning + message + follow-up),
        // each producing its own output_audio.done. We accumulate audio
        // across all items and play once on response.done so we never get
        // two playbacks for one response.
        this._activeResponseId = data.response?.id ?? "unknown";
        this._responseCreatedAt = performance.now();
        this._firstAudioAt = null;
        if (STREAMING_PLAYBACK) this._streamStart(this._activeResponseId);
        else this.audioChunks = [];
        break;
      case "response.audio.delta":
      case "response.output_audio.delta":
        if (this._firstAudioAt == null) this._firstAudioAt = performance.now();
        if (STREAMING_PLAYBACK) this._streamChunk(data.delta);
        else this.audioChunks.push(data.delta);
        break;
      case "response.audio.done":
      case "response.output_audio.done":
        // Per-output-item audio boundary — informational only. Playback is
        // deferred to response.done so multi-output responses concatenate.
        break;
      case "response.audio_transcript.done":
      case "response.output_audio_transcript.done":
        this._emit("transcript", data.transcript);
        break;
      case "response.done": {
        // End of the whole response, regardless of how many output items it
        // contained. Status can be: completed, cancelled, failed, incomplete.
        const status = data.response?.status;
        this._activeResponseId = null;
        this._logTurnTiming();
        if (status === "failed") {
          this._emit("error", {
            title: "LLM Response Failed",
            message: data.response.status_details?.error?.message ?? JSON.stringify(data.response),
          });
          if (STREAMING_PLAYBACK) this._streamFail();
          else { this.audioChunks = []; this._emit("speakingEnd"); }
          break;
        }
        if (STREAMING_PLAYBACK) {
          this._streamInputDone();
        } else if (this.audioChunks.length > 0) {
          this._playback();
        } else {
          // Response ended with no audio buffered (cancelled-early, empty, or
          // tool-call-only). Emit speakingEnd so the consumer clears any
          // "pending AI response" state.
          this._emit("speakingEnd");
        }
        break;
      }
      case "error":
        // Filter the expected race: interrupt's response.cancel reached
        // OpenAI after response.done had already arrived. Not actionable.
        if (data.error?.code === "response_cancel_not_active") {
          console.debug("[llm] response.cancel raced response.done — ignored");
          break;
        }
        this._emit("error", {
          title: "LLM Error",
          message: data.error?.message ?? JSON.stringify(data),
        });
        this._emit("speakingEnd");
        break;
      default:
        if (data.type?.startsWith("response.") || data.type?.startsWith("session.")) {
          console.debug("[llm] event:", data.type);
        }
        break;
    }
  }

  async _playback() {
    const pcm16 = this._concatBase64PCM(this.audioChunks);
    this.audioChunks = [];

    if (this.responseTimer != null) {
      const dt = (performance.now() - this.responseTimer) / 1000;
      console.log(`[llm] response time: ${dt.toFixed(2)}s`);
    }

    // Cancelled response audio arriving after we've already interrupted:
    // OpenAI may finish flushing what's in-flight before honoring
    // response.cancel, so we discard the buffered audio instead of starting
    // fresh playback on top of the user's new turn.
    if (this._suppressNextPlayback) {
      this._suppressNextPlayback = false;
      console.log("[llm] suppressed stale audio.done (post-interrupt)");
      this._emit("speakingEnd");
      return;
    }

    if (pcm16.length === 0) {
      this._emit("speakingEnd");
      return;
    }

    // Defense in depth: if a previous response is still playing (its
    // src.onended hasn't fired yet), stop it before starting a new one —
    // otherwise two AudioBufferSources end up running in parallel.
    this._stopActivePlayback();

    this._emit("speakingStart");

    const ctx = new AudioContext({ sampleRate: OUTPUT_SAMPLE_RATE });
    const f32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) f32[i] = pcm16[i] / 32768;
    const buf = ctx.createBuffer(1, f32.length, OUTPUT_SAMPLE_RATE);
    buf.copyToChannel(f32, 0);

    const src = ctx.createBufferSource();
    src.buffer = buf;
    const gain = ctx.createGain();
    gain.gain.value = Math.pow(10, this.gainDb / 20);
    src.connect(gain).connect(ctx.destination);

    // Stash on the instance so interrupt() can fade and stop mid-playback.
    this._activeCtx = ctx;
    this._activeSrc = src;
    this._activeGain = gain;

    src.onended = () => {
      ctx.close().catch(() => {});
      if (this._activeCtx === ctx) {
        this._activeCtx = null;
        this._activeSrc = null;
        this._activeGain = null;
      }
      this._emit("speakingEnd");
    };
    src.start();
  }

  // Stop any in-flight playback and tear down its AudioContext. Called from
  // _playback (defense against overlapping responses) and interrupt's hard-
  // failure fallback. Safe to call when nothing is active.
  _stopActivePlayback() {
    const src = this._activeSrc;
    const ctx = this._activeCtx;
    this._activeCtx = null;
    this._activeSrc = null;
    this._activeGain = null;
    if (src) {
      try { src.onended = null; } catch {}
      try { src.stop(); } catch {}
    }
    if (ctx) {
      try { ctx.close().catch(() => {}); } catch {}
    }
  }

  // ── Option A: incremental streaming playback ──────────────────────────
  // Deltas are scheduled back-to-back on the AudioContext clock so playback
  // starts at the first chunk. All deltas of one response share a responseId
  // and feed ONE gapless stream (the anti-overlap invariant); a new responseId
  // stops the prior stream first.

  _ensurePlayCtx() {
    if (!this._playCtx) this._playCtx = new AudioContext({ sampleRate: OUTPUT_SAMPLE_RATE });
    if (this._playCtx.state === "suspended") this._playCtx.resume().catch(() => {});
    return this._playCtx;
  }

  _streamStart(id) {
    if (this._stream && this._stream.id !== id) this._stopStream(this._stream);
    this._stream = {
      id, cursor: 0, pending: 0, inputDone: false,
      speaking: false, aborted: false, finished: false,
      gain: null, sources: new Set(),
    };
  }

  _streamChunk(b64) {
    const s = this._stream;
    if (!s || s.aborted) return;
    const pcm16 = this._concatBase64PCM([b64]);
    if (pcm16.length === 0) return;
    const ctx = this._ensurePlayCtx();
    if (!s.gain) {
      s.gain = ctx.createGain();
      s.gain.gain.value = Math.pow(10, this.gainDb / 20);
      s.gain.connect(ctx.destination);
      s.cursor = ctx.currentTime + STREAM_LEAD_IN;
    }
    const f32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) f32[i] = pcm16[i] / 32768;
    const buf = ctx.createBuffer(1, f32.length, OUTPUT_SAMPLE_RATE);
    buf.copyToChannel(f32, 0);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(s.gain);
    const startAt = Math.max(s.cursor, ctx.currentTime); // underrun guard
    src.start(startAt);
    s.cursor = startAt + buf.duration;
    s.sources.add(src);
    s.pending++;
    if (!s.speaking) { s.speaking = true; this._emit("speakingStart"); }
    src.onended = () => {
      s.sources.delete(src);
      s.pending--;
      if (s.pending === 0 && s.inputDone) this._finishStream(s);
    };
  }

  _streamInputDone() {
    const s = this._stream;
    if (!s) return;
    s.inputDone = true;
    if (s.pending === 0) this._finishStream(s);
  }

  _streamFail() {
    const s = this._stream;
    if (s) { s.aborted = true; this._finishStream(s); }
    else this._emit("speakingEnd");
  }

  _streamInterrupt(fadeMs) {
    if (this._activeResponseId && this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(JSON.stringify({ type: "response.cancel" })); } catch {}
    }
    const s = this._stream;
    if (!s || s.aborted || !s.gain || s.pending === 0) {
      if (s) { s.aborted = true; this._finishStream(s); }
      else this._emit("speakingEnd");
      return;
    }
    s.aborted = true;
    s.inputDone = true;
    const ctx = this._playCtx;
    const fadeSec = Math.max(0, fadeMs) / 1000;
    const now = ctx.currentTime;
    try {
      s.gain.gain.cancelScheduledValues(now);
      s.gain.gain.setValueAtTime(s.gain.gain.value, now);
      s.gain.gain.linearRampToValueAtTime(0, now + fadeSec);
      for (const src of s.sources) { try { src.stop(now + fadeSec); } catch {} }
    } catch {
      this._finishStream(s);
    }
  }

  _stopStream(s) {
    if (!s || s.finished) return;
    s.finished = true;
    for (const src of s.sources) { try { src.onended = null; src.stop(); } catch {} }
    s.sources.clear();
    if (s.gain) { try { s.gain.disconnect(); } catch {} }
    if (this._stream === s) this._stream = null;
  }

  _finishStream(s) {
    if (!s || s.finished) return;
    s.finished = true;
    for (const src of s.sources) { try { src.onended = null; src.stop(); } catch {} }
    s.sources.clear();
    if (s.gain) { try { s.gain.disconnect(); } catch {} }
    if (this._stream === s) this._stream = null;
    this._emit("speakingEnd");
  }

  // Fade the currently-playing response to silence over `fadeMs` and (when
  // a response is still in flight) cancel upstream OpenAI generation.
  // Wire up to client.on("interrupt", (e) => llm.interrupt(e.fadeMs)) when
  // building a barge-in-capable demo.
  interrupt(fadeMs = 500) {
    if (STREAMING_PLAYBACK) { this._streamInterrupt(fadeMs); return; }
    // Drop any buffered chunks that haven't been concatenated into a playback
    // buffer yet, and suppress the audio.done that OpenAI may still deliver
    // after we've sent response.cancel.
    this.audioChunks = [];
    this._suppressNextPlayback = true;

    // Cancel upstream — only if a response is actually in flight. Once
    // response.done has landed (we're just playing buffered audio locally),
    // OpenAI rejects response.cancel with response_cancel_not_active.
    if (this._activeResponseId && this.ws && this.ws.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(JSON.stringify({ type: "response.cancel" }));
      } catch {}
    }

    if (!this._activeSrc || !this._activeGain || !this._activeCtx) {
      // Nothing currently playing — still emit speakingEnd so the consumer
      // unwinds responding state. Matches the natural-end code path.
      this._emit("speakingEnd");
      return;
    }

    const ctx = this._activeCtx;
    const src = this._activeSrc;
    const gain = this._activeGain;
    const fadeSec = Math.max(0, fadeMs) / 1000;
    const now = ctx.currentTime;

    try {
      gain.gain.cancelScheduledValues(now);
      gain.gain.setValueAtTime(gain.gain.value, now);
      gain.gain.linearRampToValueAtTime(0, now + fadeSec);
      src.stop(now + fadeSec);
    } catch (err) {
      console.warn("[llm] interrupt fade failed:", err);
      this._stopActivePlayback();
      this._emit("speakingEnd");
      return;
    }
    // src.onended will fire when stop() lands and emit speakingEnd.
  }

  // P0 latency breakdown for one response, logged at response.done.
  // Intervals: sent→created = LLM ack (response.created), sent→first_audio =
  // time to first audio token, first_audio→done = how long audio sat before
  // we play it. `buffered` is the UPPER BOUND on what Option A (incremental
  // playback) could reclaim — actual savings are a bit less since audio
  // decode + scheduling at playback start aren't subtracted. All stamps are
  // perf-clock (client-side, skew-free); bridge-internal latency, separate
  // from the SDK's server→client transit.
  _logTurnTiming() {
    const sent = this.responseTimer;
    if (sent == null) return;
    const now = performance.now();
    const created = this._responseCreatedAt;
    const first = this._firstAudioAt;
    const ms = (x) => (x == null ? NaN : Math.round(x));
    const ack = created != null ? created - sent : NaN;
    const ttfb = first != null ? first - sent : NaN;
    const buffered = first != null ? now - first : 0;
    const total = now - sent;
    const tail = STREAMING_PLAYBACK
      ? `time-to-sound≈${ms(ttfb)}ms (streaming)`
      : `(Option-A headroom ≤${ms(buffered)}ms)`;
    console.log(
      `[llm-timing] sent→created=${ms(ack)}ms ttfb(sent→first_audio)=${ms(ttfb)}ms ` +
      `buffered(first_audio→done)=${ms(buffered)}ms total=${ms(total)}ms ${tail}`,
    );
    this.responseTimer = null;
    this._responseCreatedAt = null;
    this._firstAudioAt = null;
  }

  _concatBase64PCM(b64List) {
    let total = 0;
    const bins = b64List.map((b64) => {
      const s = atob(b64);
      total += s.length;
      return s;
    });
    const bytes = new Uint8Array(total);
    let off = 0;
    for (const s of bins) {
      for (let i = 0; i < s.length; i++) bytes[off + i] = s.charCodeAt(i);
      off += s.length;
    }
    return new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
  }

  close() {
    this.closed = true;
    this.pendingAudio = null;
    this.pendingFrames = [];
    this.pendingGreeting = null;
    this._suppressNextPlayback = false;
    this._activeResponseId = null;
    this.audioChunks = [];
    this._stopActivePlayback();
    if (this._stream) this._stopStream(this._stream);
    if (this._playCtx) { try { this._playCtx.close(); } catch {} this._playCtx = null; }
    if (this.ws) {
      try { this.ws.close(); } catch {}
      this.ws = null;
    }
    this.sessionReady = false;
  }
}
