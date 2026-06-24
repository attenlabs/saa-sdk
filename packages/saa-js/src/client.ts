import type {
  AttentionClientOptions,
  AttentionEventMap,
  AttentionEventName,
  AttentionListener,
  StartOptions,
} from "./types.js";
import {
  MSG_AUDIO,
  MSG_VIDEO,
  base64ToInt16,
  frameBinary,
  type ServerMessage,
} from "./ws-protocol.js";
import {
  createAudioPipeline,
  startVideoPipeline,
  toFloat32Mono,
  resampleLinear,
  floatToPcm16,
  TARGET_SAMPLE_RATE,
  SEND_INTERVAL_SAMPLES,
  type AudioPipeline,
  type VideoPipeline,
} from "./capture.js";
import { applyServerProfileToWsUrl, allocateBody } from "./url.js";

const WS_PING_INTERVAL_MS = 5000;
const WS_PONG_TIMEOUT_MS = 15000;
const WS_STATS_INTERVAL_MS = 10000;
const DEFAULT_THRESHOLD = 0.7;
const DEFAULT_SERVER_URL = "https://broker.attentionlabs.ai";

// Full-jitter reconnect backoff
const RECONNECT_BASE_S = 0.5;
const RECONNECT_CAP_S = 20;
// Close codes that are NOT worth retrying — give up and surface the error.
const FATAL_CLOSE_CODES = new Set([1000, 1002, 1003, 1007, 1008, 1009, 1010, 1015]);

type AnyListener = (payload?: unknown) => void;

export class AttentionClient {
  private readonly opts: AttentionClientOptions;
  private readonly listeners = new Map<AttentionEventName, Set<AnyListener>>();

  private ws: WebSocket | null = null;
  private mediaStream: MediaStream | null = null;
  private audioPipeline: AudioPipeline | null = null;
  private videoPipeline: VideoPipeline | null = null;

  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private statsTimer: ReturnType<typeof setInterval> | null = null;
  private visibilityHandler: (() => void) | null = null;
  private lastPingAt = 0;
  private lastPongAt = 0;
  private lastRttMs: number | null = null;
  private wsOpenedAt = 0;

  private sentVideo = 0;
  private skippedVideo = 0;
  private sentAudio = 0;

  private micMuted = false;
  private warmedUp = false;
  private threshold: number;
  private started = false;

  // emit "Connection Stalled" at most once per stall episode; reset on pong
  private stallEmitted = false;

  // reconnect state
  private readonly autoReconnect: boolean;
  private reconnecting = false;
  private stopping = false;
  private reconnectAttempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  // http origin derived from the resolved ws url — used by the client-log beacon
  private httpOrigin: string | null = null;

  private readonly enableAudio: boolean;
  private readonly enableVideo: boolean;
  // false for a caller-supplied stream — stop() won't stop its tracks
  private ownsStream = true;
  private feedBuffer = new Float32Array(0); // feedAudio() carry-over

  // server-assigned id
  private sessionId: string | null = null;

  constructor(opts: AttentionClientOptions) {
    this.opts = opts;
    this.threshold = clamp01(opts.initialThreshold ?? DEFAULT_THRESHOLD);
    this.enableAudio = opts.enableAudio !== false;
    this.enableVideo = opts.enableVideo !== false;
    this.autoReconnect = opts.autoReconnect !== false;
  }

  on<E extends AttentionEventName>(
    event: E,
    listener: AttentionListener<E>,
  ): () => void {
    let set = this.listeners.get(event);
    if (!set) {
      set = new Set();
      this.listeners.set(event, set);
    }
    set.add(listener as AnyListener);
    return () => this.off(event, listener);
  }

  off<E extends AttentionEventName>(
    event: E,
    listener: AttentionListener<E>,
  ): void {
    this.listeners.get(event)?.delete(listener as AnyListener);
  }

  private emit<E extends AttentionEventName>(
    event: E,
    payload?: AttentionEventMap[E],
  ): void {
    const set = this.listeners.get(event);
    if (!set) return;
    for (const fn of set) {
      try {
        fn(payload);
      } catch (err) {
        console.error(`[saa-js] listener for '${event}' threw:`, err);
      }
    }
  }

  get isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  get currentThreshold(): number {
    return this.threshold;
  }

  async start(options: StartOptions = {}): Promise<void> {
    if (this.started) throw new Error("AttentionClient already started");
    this.started = true;
    // reset reconnect state here (not in stop()) so a late reconnect callback
    // after stop() still sees stopping=true and bails
    this.stopping = false;
    this.reconnecting = false;
    this.reconnectAttempt = 0;

    const videoEl = options.videoElement ?? null;
    const videoOpts = this.opts.video ?? {};
    const audioOpts = this.opts.audio ?? {};

    if (this.enableVideo && !videoEl) {
      this.started = false;
      throw new Error(
        "start() needs options.videoElement when video capture is enabled — " +
          "pass enableVideo:false for audio-only or feedVideo() mode",
      );
    }

    // caller-supplied stream skips getUserMedia; both disabled = no capture
    try {
      if (options.mediaStream) {
        this.mediaStream = options.mediaStream;
        this.ownsStream = false;
      } else if (this.enableAudio || this.enableVideo) {
        this.mediaStream = await navigator.mediaDevices.getUserMedia({
          audio: this.enableAudio
            ? { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
            : false,
          video: this.enableVideo
            ? {
                width: { ideal: videoOpts.width ?? 1920, max: videoOpts.width ?? 1920 },
                height: { ideal: videoOpts.height ?? 1080, max: videoOpts.height ?? 1080 },
              }
            : false,
        });
        this.ownsStream = true;
      }
    } catch (err) {
      this.started = false;
      throw err;
    }

    if (this.enableVideo && videoEl && this.mediaStream) {
      videoEl.srcObject = this.mediaStream;
      if (!videoEl.videoWidth) {
        await new Promise<void>((resolve) =>
          videoEl.addEventListener("loadedmetadata", () => resolve(), {
            once: true,
          }),
        );
      }
    }

    try {
      await this.connectWS();
    } catch (err) {
      this.teardownMedia();
      this.started = false;
      throw err;
    }

    if (this.enableAudio && this.mediaStream) {
      try {
        // surface worklet/context errors as `error` events, then call the user's hooks
        const userOnWorkletError = audioOpts.onWorkletError;
        const userOnContextStateChange = audioOpts.onContextStateChange;
        const wiredAudioOpts: typeof audioOpts = {
          ...audioOpts,
          onWorkletError: (err: unknown) => {
            this.emit("error", {
              title: "Audio worklet error",
              message: "The audio capture worklet threw and may have stopped streaming.",
              detail: describeError(err),
              kind: "audio",
              retriable: false,
            });
            try {
              userOnWorkletError?.(err);
            } catch {}
          },
          onContextStateChange: (state: string) => {
            if (state === "suspended" || state === "interrupted") {
              this.emit("error", {
                title: "Audio paused",
                message: `Microphone capture is ${state}. Audio may not be reaching the server.`,
                detail: `AudioContext.state=${state}`,
                kind: "audio",
                retriable: false,
              });
            }
            try {
              userOnContextStateChange?.(state);
            } catch {}
          },
        };
        this.audioPipeline = await createAudioPipeline(
          this.mediaStream,
          this.opts.workletUrl,
          wiredAudioOpts,
          (pcm16) => this.sendAudio(pcm16),
        );
      } catch (err) {
        await this.stop();
        throw err;
      }
    }

    if (this.enableVideo && videoEl) {
      this.videoPipeline = startVideoPipeline(
        videoEl,
        videoOpts,
        () => this.ws?.bufferedAmount ?? 0,
        () => this.isConnected,
        (jpeg) => this.sendVideo(jpeg),
        () => {
          this.skippedVideo++;
        },
      );
    }

    // Backgrounded tabs are the most common cause of unclean disconnects:
    // Chrome clamps setInterval to ~1Hz when a tab loses focus and AudioContext
    // can be suspended outright, so we end up sending almost no media and the
    // server's stall watchdog (or an intermediate proxy) eventually drops the
    // socket. Surface a clear warning the moment visibility flips so the user
    // sees something actionable before the disconnect lands.
    if (typeof document !== "undefined" &&
        typeof document.addEventListener === "function") {
      this.visibilityHandler = () => {
        if (document.visibilityState === "hidden" && this.isConnected) {
          this.emit("error", {
            title: "Tab Hidden",
            message: "Browsers throttle audio and video when this tab is in the background. Keep the tab visible to stay connected.",
            detail: null,
            kind: "environment",
            retriable: false,
          });
        }
      };
      document.addEventListener("visibilitychange", this.visibilityHandler);
    }
  }

  async stop(): Promise<void> {
    // set stopping FIRST so an in-flight onclose/backoff bails out of reconnect
    this.stopping = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.reconnecting = false;
    this.reconnectAttempt = 0;
    if (this.visibilityHandler &&
        typeof document !== "undefined" &&
        typeof document.removeEventListener === "function") {
      document.removeEventListener("visibilitychange", this.visibilityHandler);
    }
    this.visibilityHandler = null;
    if (this.videoPipeline) {
      this.videoPipeline.stop();
      this.videoPipeline = null;
    }
    if (this.audioPipeline) {
      await this.audioPipeline.close();
      this.audioPipeline = null;
    }
    this.teardownMedia();
    this.stopHeartbeat();
    if (this.ws) {
      try {
        this.ws.close(1000, "client stop");
      } catch {}
      this.ws = null;
    }
    this.started = false;
    this.warmedUp = false;
    this.sessionId = null;
    this.micMuted = false;
    this.feedBuffer = new Float32Array(0);
    // leave stopping=true; start() resets it. A reconnect in-flight during
    // stop() must keep seeing stopping=true until a fresh start().
  }

  mute(): void {
    this.micMuted = true;
    this.sendControl({ action: "mute" });
  }

  unmute(): void {
    this.micMuted = false;
    this.sendControl({ action: "unmute" });
  }

  /** Push external audio (requires enableAudio:false); resampled + re-chunked to 16 kHz/100 ms */
  feedAudio(
    audio: Int16Array | Float32Array | ArrayBuffer | ArrayBufferView,
    sampleRate: number = TARGET_SAMPLE_RATE,
  ): void {
    if (this.enableAudio) {
      throw new Error(
        "feedAudio() requires enableAudio:false — the SDK is capturing its own mic",
      );
    }
    if (!this.started) throw new Error("call start() before feedAudio()");

    let samples = toFloat32Mono(audio);
    if (samples.length === 0) return;
    if (sampleRate !== TARGET_SAMPLE_RATE) {
      samples = resampleLinear(samples, sampleRate, TARGET_SAMPLE_RATE);
    }

    const combined = new Float32Array(this.feedBuffer.length + samples.length);
    combined.set(this.feedBuffer);
    combined.set(samples, this.feedBuffer.length);

    let offset = 0;
    while (combined.length - offset >= SEND_INTERVAL_SAMPLES) {
      const chunk = combined.subarray(offset, offset + SEND_INTERVAL_SAMPLES);
      offset += SEND_INTERVAL_SAMPLES;
      // .buffer widens to ArrayBufferLike on TS 5.7+; it's a real ArrayBuffer at runtime
      this.sendAudio(floatToPcm16(chunk).buffer as ArrayBuffer);
    }
    this.feedBuffer = combined.slice(offset);
  }

  /** Push an external JPEG frame (requires enableVideo:false) */
  feedVideo(jpeg: Blob | ArrayBuffer | ArrayBufferView): void {
    if (this.enableVideo) {
      throw new Error(
        "feedVideo() requires enableVideo:false — the SDK is capturing its own camera",
      );
    }
    if (!this.started) throw new Error("call start() before feedVideo()");

    if (jpeg instanceof Blob) {
      jpeg
        .arrayBuffer()
        .then((buf) => this.sendVideo(buf))
        .catch(() => {});
      return;
    }
    const buf =
      jpeg instanceof ArrayBuffer
        ? jpeg
        : jpeg.buffer.slice(jpeg.byteOffset, jpeg.byteOffset + jpeg.byteLength);
    this.sendVideo(buf as ArrayBuffer);
  }

  markResponding(responding: boolean): void {
    this.sendControl({
      action: responding ? "responding_start" : "responding_stop",
    });
  }

  setThreshold(value: number): void {
    const next = clamp01(value);
    this.threshold = next;
    this.sendControl({ action: "set_threshold", value: next });
  }

  /**
   * Forward a batch of browser log entries to the server. Prefers the live WS
   * (control frame); when it's closed, dispatches a best-effort HTTP beacon to
   * the resolved origin's /client_log. Returns true if dispatched either way.
   *
   * shape (flexible)
   *   { ts, wallclock_ts, level, category, msg, stack?, context?, count? }
   */
  sendClientLog(entries: ReadonlyArray<Record<string, unknown>>): boolean {
    if (!entries || entries.length === 0) return true;  // nothing to do, considered success

    if (this.sendControl({ action: "client_log", entries })) return true;

    // WS closed (or send threw) — fall back to an HTTP beacon.
    if (!this.httpOrigin) return false;
    const endpoint = `${this.httpOrigin}/client_log`;
    const body = JSON.stringify({ entries });

    // navigator.sendBeacon survives page unload but can't set headers, so the
    // bearer token rides along only on the fetch path.
    try {
      const beacon =
        typeof navigator !== "undefined" &&
        typeof navigator.sendBeacon === "function";
      if (beacon) {
        const blob = new Blob([body], { type: "application/json" });
        if (navigator.sendBeacon(endpoint, blob)) return true;
      }
    } catch {}

    try {
      if (typeof fetch === "function") {
        const headers: Record<string, string> = { "Content-Type": "application/json" };
        if (this.opts.token) headers["Authorization"] = `Bearer ${this.opts.token}`;
        // keepalive lets the POST outlive an unloading page
        void fetch(endpoint, { method: "POST", keepalive: true, headers, body }).catch(() => {});
        return true;
      }
    } catch {}

    return false;
  }

  getSessionId(): string | null {
    return this.sessionId;
  }

  private teardownMedia(): void {
    if (this.mediaStream) {
      // only stop tracks we created
      if (this.ownsStream) {
        for (const t of this.mediaStream.getTracks()) t.stop();
      }
      this.mediaStream = null;
    }
    this.ownsStream = true;
  }

  /**
   * Resolve `opts.url` to a concrete `wss://…/ws` URL.
   *
   * - `ws(s)://…` is treated as a direct backend URL — returned as-is.
   * - `http(s)://…` is treated as a broker base URL — POST /allocate
   *   with the bearer token, return the wss URL the broker hands back.
   *
   * `start()` calls this once per WS connect, so reconnects pick a fresh
   * least-loaded backend each time.
   */
  private async resolveWsUrl(): Promise<string> {
    const url = this.opts.url ?? DEFAULT_SERVER_URL;
    if (url.startsWith("ws://") || url.startsWith("wss://")) {
      //  bake the server_profile into the query (the backend
      // /ws reads it); precedence handled in applyServerProfileToWsUrl.
      const resolved = applyServerProfileToWsUrl(url, this.opts.serverProfile, this.enableVideo);
      this.httpOrigin = wsUrlToHttpOrigin(resolved);
      return resolved;
    }
    const allocateUrl = `${url.replace(/\/$/, "")}/allocate`;
    const headers: Record<string, string> = {};
    if (this.opts.token) {
      headers["Authorization"] = `Bearer ${this.opts.token}`;
    }
    // broker bakes the selector into the wss URL it hands back; empty body =
    // legacy default profile.
    const body = allocateBody(this.opts.serverProfile, this.enableVideo);
    if (body) headers["Content-Type"] = "application/json";
    const r = await fetch(allocateUrl, { method: "POST", headers, body });
    if (!r.ok) {
      const body = await r.text().catch(() => "");
      throw new Error(
        `broker /allocate failed: HTTP ${r.status} ${body || r.statusText}`,
      );
    }
    const payload = (await r.json()) as { url?: string };
    if (!payload.url) {
      throw new Error("broker /allocate returned no url");
    }
    this.httpOrigin = wsUrlToHttpOrigin(payload.url);
    return payload.url;
  }

  private async connectWS(): Promise<void> {
    const url = await this.resolveWsUrl();
    return new Promise((resolve, reject) => {
      const protocols = this.opts.token ? [this.opts.token] : undefined;
      const ws = new WebSocket(url, protocols);
      ws.binaryType = "arraybuffer";
      this.ws = ws;

      let settled = false;

      ws.onopen = () => {
        this.wsOpenedAt = performance.now();
        this.sentAudio = 0;
        this.sentVideo = 0;
        this.skippedVideo = 0;
        this.lastPongAt = this.wsOpenedAt;
        this.stallEmitted = false;
        this.startHeartbeat();
        this.emit("connected");
        if (!settled) {
          settled = true;
          resolve();
        }
      };

      ws.onmessage = (e) => {
        if (typeof e.data !== "string") return;
        let msg: ServerMessage;
        try {
          msg = JSON.parse(e.data) as ServerMessage;
        } catch {
          return;
        }
        if (msg.type === "pong") {
          this.lastPongAt = performance.now();
          this.stallEmitted = false;
          if (typeof msg.client_ts === "number") {
            this.lastRttMs = this.lastPongAt - msg.client_ts;
          }
          return;
        }
        this.handleServerMessage(msg);
      };

      ws.onerror = () => {
        // Browser hides details; rely on onclose for the real reason.
      };

      ws.onclose = (e) => {
        this.stopHeartbeat();
        this.ws = null;

        // a failed INITIAL handshake rejects out of start() — never reconnect
        if (!settled) {
          settled = true;
          reject(buildCloseError(e.code, e.reason, e.wasClean));
          return;
        }

        // unclean mid-session drop is the lifecycle event; always emit it
        this.emit("disconnected", {
          code: e.code,
          reason: e.reason || "",
          wasClean: e.wasClean,
        });

        const code = normalizeCloseCode(e.code);
        const willReconnect =
          this.autoReconnect && !this.stopping && isRetriableCode(code);

        // B8: suppress the scary error when we're about to reconnect — let
        // reconnecting/reconnected tell the story. Otherwise emit it.
        if (!willReconnect) {
          const err = buildCloseError(e.code, e.reason, e.wasClean);
          if (err) this.emit("error", err);
        }

        if (willReconnect) this.scheduleReconnect(code);
      };
    });
  }

  /**
   * Schedule a backoff reconnect after an unclean mid-session drop. Single-
   * threaded: a setTimeout sleep (interruptible by stop()) then a fresh
   * connectWS(). Re-resolves the URL each attempt so the broker can re-pick a
   * least-loaded backend. Persistent mic/cam/heartbeat pipelines read `this.ws`
   * live, so they pause during the gap and resume on the new socket.
   */
  private scheduleReconnect(lastCode: number): void {
    if (this.reconnecting || this.stopping) return;
    this.reconnecting = true;

    const attemptOnce = () => {
      if (this.stopping) {
        this.reconnecting = false;
        return;
      }
      const k = this.reconnectAttempt;
      const delaySec = Math.random() * Math.min(RECONNECT_CAP_S, RECONNECT_BASE_S * 2 ** k);
      this.emit("reconnecting", {
        attempt: k + 1,
        delaySec,
        lastCode,
      });
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        if (this.stopping) {
          this.reconnecting = false;
          return;
        }
        this.connectWS().then(
          () => {
            // stop() may have landed while connecting — tear the fresh socket
            // down rather than bring it up on a stopped client
            if (this.stopping) {
              this.reconnecting = false;
              try { this.ws?.close(1000, "client stop"); } catch {}
              this.ws = null;
              return;
            }
            // onopen already fired; the started-handler resync re-applies state
            const attempts = this.reconnectAttempt + 1;
            this.reconnectAttempt = 0;
            this.reconnecting = false;
            this.emit("reconnected", { attempts });
          },
          () => {
            // failed connect — back off and try again until stopped
            this.reconnectAttempt += 1;
            this.reconnecting = false;
            this.scheduleReconnect(lastCode);
          },
        );
      }, delaySec * 1000);
    };

    attemptOnce();
  }

  private handleServerMessage(msg: ServerMessage): void {
    switch (msg.type) {
      case "prediction": {
        // Prefer the server's display_class (e.g. low-conf class-2 relabelled
        // to class-1). Falls back to raw `class` for older servers.
        const cls = msg.display_class ?? msg.class ?? 0;
        const conf = msg.confidence ?? 0;
        this.emit("prediction", {
          cls,
          rawCls: typeof msg.class === "number" ? msg.class : null,
          confidence: conf,
          source: msg.source,
          numFaces: msg.num_faces,
          responding: msg.responding ?? msg.source === "ai_responding",
        });
        break;
      }
      case "vad":
        this.emit("vad", {
          probability: msg.probability,
          isSpeech: msg.is_speech,
        });
        break;
      case "state":
        this.emit("state", { state: msg.state });
        break;
      case "turn_ready":
        this.emit("turnReady", {
          audioBase64: msg.audio_base64,
          audioPcm16: base64ToInt16(msg.audio_base64),
          durationSec: msg.duration,
          // Server-clock emit stamp
          serverTurnReadyTsMs: typeof (msg as any).server_turn_ready_ts_ms === "number"
            ? (msg as any).server_turn_ready_ts_ms : null,
          frames: (msg.frames ?? []).map((f) => ({
            tsOffsetS: f.ts_offset_s,
            imageBase64: f.image_base64,
          })),
          context: typeof msg.context === "string" ? msg.context : null,
        });
        break;
      case "started":
        if (typeof msg.session_id === "string") {
          this.sessionId = msg.session_id;
        }
        this.emit("started");
        // `started` only means the model is loaded and session has started.
        // Re-push threshold + mute here so a reconnected session restores state
        // uniformly with the initial one (no separate resync path).
        this.sendControl({ action: "set_threshold", value: this.threshold });
        if (this.micMuted) this.sendControl({ action: "mute" });
        break;
      case "warmup_complete":
        if (!this.warmedUp) {
          this.warmedUp = true;
          this.emit("warmupComplete");
        }
        break;
      case "config":
        if (typeof msg.model_class2_threshold === "number") {
          this.threshold = msg.model_class2_threshold;
          this.emit("config", {
            modelClass2Threshold: msg.model_class2_threshold,
          });
        }
        break;
      case "interrupt":
        this.emit("interrupt", {
          fadeMs: typeof msg.fade_ms === "number" ? msg.fade_ms : 500,
          confidence: typeof msg.confidence === "number" ? msg.confidence : 0.85,
        });
        break;
      case "interjection":
        // Server's InterjectionDetector fired the P3 pattern (humans were
        // chatting then went quiet, faces still in frame). Route the
        // recent conversation audio to the LLM with a per-reason system
        // instruction asking for a brief volunteer. SDK already self-
        // marked its cooldown clock — no ack needed upstream.
        this.emit("interjection", {
          reason: msg.reason,
          audioBase64: msg.audio_base64,
          audioPcm16: base64ToInt16(msg.audio_base64),
          durationSec: msg.duration_s,
        });
        break;
      case "error":
        this.emit("error", {
          title: "Server Error",
          message: msg.message,
          detail: msg.detail ?? null,
          kind: "server",
          retriable: false,
        });
        break;
    }
  }

  private sendAudio(pcm16: ArrayBuffer): void {
    if (!this.isConnected) return;
    try {
      this.ws!.send(frameBinary(MSG_AUDIO, pcm16));
      this.sentAudio++;
    } catch {
      // Connection is mid-close; drop silently.
    }
  }

  private sendVideo(jpeg: ArrayBuffer): void {
    if (!this.isConnected) {
      this.skippedVideo++;
      return;
    }
    try {
      this.ws!.send(frameBinary(MSG_VIDEO, jpeg));
      this.sentVideo++;
    } catch {
      this.skippedVideo++;
    }
  }

  private sendControl(data: Record<string, unknown>): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    try {
      this.ws.send(JSON.stringify(data));
      return true;
    } catch {
      return false;
    }
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.pingTimer = setInterval(() => {
      if (!this.isConnected) return;
      if (
        this.lastPongAt &&
        performance.now() - this.lastPongAt > WS_PONG_TIMEOUT_MS
      ) {
        // emit at most once per stall episode (latch reset on pong/open)
        if (!this.stallEmitted) {
          this.stallEmitted = true;
          this.emit("error", {
            title: "Connection Stalled",
            message: "No pong received within timeout window.",
            detail: `${((performance.now() - this.lastPongAt) / 1000).toFixed(1)}s since last pong`,
            kind: "transport",
            retriable: true,
          });
          // force the half-open socket closed so reconnect/teardown takes over
          try {
            this.ws?.close(4000, "stall");
          } catch {}
        }
        return; // stop pinging a dead socket
      }
      this.lastPingAt = performance.now();
      this.sendControl({ action: "ping", ts: this.lastPingAt });
    }, WS_PING_INTERVAL_MS);

    this.statsTimer = setInterval(() => {
      if (!this.isConnected) return;
      this.emit("stats", {
        rttMs: this.lastRttMs,
        bufferedAmount: this.ws?.bufferedAmount ?? 0,
        sentVideo: this.sentVideo,
        skippedVideo: this.skippedVideo,
        sentAudio: this.sentAudio,
        uptimeMs: this.wsOpenedAt ? performance.now() - this.wsOpenedAt : 0,
      });
    }, WS_STATS_INTERVAL_MS);
  }

  private stopHeartbeat(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    if (this.statsTimer) {
      clearInterval(this.statsTimer);
      this.statsTimer = null;
    }
  }
}

function clamp01(n: number): number {
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.min(1, n));
}

function describeError(err: unknown): string {
  if (!err) return "unknown";
  if (err instanceof Error) return err.message || err.name || "Error";
  if (typeof err === "string") return err;
  if (typeof err === "object" && err && "type" in err) {
    return `Event<${String((err as { type: unknown }).type)}>`;
  }
  try {
    return JSON.stringify(err);
  } catch {
    return String(err);
  }
}

function buildCloseError(
  code: number,
  reason: string,
  wasClean: boolean,
): AttentionEventMap["error"] | null {
  if (code === 1000) return null;
  if (code === 1008)
    return {
      title: "Auth Failed",
      message: "Server rejected the auth token.",
      detail: reason || `close code ${code}`,
      code,
      kind: "auth",
      retriable: false,
    };
  if (code === 1013)
    return {
      title: "Rate Limited",
      message: "Throttled by server — try again shortly.",
      detail: reason || `close code ${code}`,
      code,
      kind: "rate_limit",
      retriable: true,
    };
  if (code === 1006 || code === 0)
    return {
      title: "Connection Failed",
      message: "Could not reach the server.",
      detail: `The server may be down or unreachable. (close code ${code})`,
      code,
      kind: "transport",
      retriable: true,
    };
  if (!wasClean)
    return {
      title: "Disconnected",
      message: "Connection lost unexpectedly.",
      detail: `code=${code} reason=${reason || "none"}`,
      code,
      kind: "transport",
      retriable: true,
    };
  return null;
}

function normalizeCloseCode(code: number): number {
  // code=0 / no-code sentinel normalizes to the abnormal-closure code 1006
  return code === 0 ? 1006 : code;
}

function isRetriableCode(code: number): boolean {
  return !FATAL_CLOSE_CODES.has(code);
}

function wsUrlToHttpOrigin(wsUrl: string): string | null {
  try {
    const u = new URL(wsUrl);
    u.protocol = u.protocol === "wss:" ? "https:" : "http:";
    return u.origin;
  } catch {
    return null;
  }
}
