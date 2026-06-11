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
  type AudioPipeline,
  type VideoPipeline,
} from "./capture.js";

const WS_PING_INTERVAL_MS = 5000;
const WS_PONG_TIMEOUT_MS = 15000;
const WS_STATS_INTERVAL_MS = 10000;
const DEFAULT_THRESHOLD = 0.7;
const DEFAULT_SERVER_URL = "https://broker.attentionlabs.ai";

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
  
  // server-assigned id
  private sessionId: string | null = null;

  constructor(opts: AttentionClientOptions) {
    this.opts = opts;
    this.threshold = clamp01(opts.initialThreshold ?? DEFAULT_THRESHOLD);
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

  async start(options: StartOptions): Promise<void> {
    if (this.started) throw new Error("AttentionClient already started");
    this.started = true;

    const videoEl = options.videoElement;
    const videoOpts = this.opts.video ?? {};
    const audioOpts = this.opts.audio ?? {};

    try {
      this.mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: {
          width: { ideal: videoOpts.width ?? 1920, max: videoOpts.width ?? 1920 },
          height: {
            ideal: videoOpts.height ?? 1080,
            max: videoOpts.height ?? 1080,
          },
        },
      });
    } catch (err) {
      this.started = false;
      throw err;
    }

    videoEl.srcObject = this.mediaStream;
    if (!videoEl.videoWidth) {
      await new Promise<void>((resolve) =>
        videoEl.addEventListener("loadedmetadata", () => resolve(), {
          once: true,
        }),
      );
    }

    try {
      await this.connectWS();
    } catch (err) {
      this.teardownMedia();
      this.started = false;
      throw err;
    }

    try {
      // Layer SDK error/state hooks on top of any caller-supplied callbacks so
      // we always surface worklet failures and AudioContext suspensions as
      // `error` events without messing up user-provided handlers.
      const userOnWorkletError = audioOpts.onWorkletError;
      const userOnContextStateChange = audioOpts.onContextStateChange;
      const wiredAudioOpts: typeof audioOpts = {
        ...audioOpts,
        onWorkletError: (err: unknown) => {
          this.emit("error", {
            title: "Audio worklet error",
            message: "The audio capture worklet threw and may have stopped streaming.",
            detail: describeError(err),
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
          });
        }
      };
      document.addEventListener("visibilitychange", this.visibilityHandler);
    }
  }

  async stop(): Promise<void> {
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
  }

  mute(): void {
    this.micMuted = true;
    this.sendControl({ action: "mute" });
  }

  unmute(): void {
    this.micMuted = false;
    this.sendControl({ action: "unmute" });
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
   * Forward a batch of browser log entries over the active WebSocket. 
   * Returns true if the send was queued, false if the WS isn't open, 
   * falls back to an HTTP beacon in that case.
   *
   * shape (flexible)
   *   { ts, wallclock_ts, level, category, msg, stack?, context?, count? }
   */
  sendClientLog(entries: ReadonlyArray<Record<string, unknown>>): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    if (!entries || entries.length === 0) return true;  // nothing to do, considered success

    // if ws.send synchronously throws (e.g. mid-teardown), 
    // return false so the caller can fall back to the HTTP beacon.
    return this.sendControl({ action: "client_log", entries });
  }

  getSessionId(): string | null {
    return this.sessionId;
  }

  private teardownMedia(): void {
    if (this.mediaStream) {
      for (const t of this.mediaStream.getTracks()) t.stop();
      this.mediaStream = null;
    }
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
      return url;
    }
    const allocateUrl = `${url.replace(/\/$/, "")}/allocate`;
    const headers: Record<string, string> = {};
    if (this.opts.token) {
      headers["Authorization"] = `Bearer ${this.opts.token}`;
    }
    const r = await fetch(allocateUrl, { method: "POST", headers });
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

        this.emit("disconnected", {
          code: e.code,
          reason: e.reason || "",
          wasClean: e.wasClean,
        });

        if (!settled) {
          settled = true;
          reject(
            buildCloseError(e.code, e.reason, e.wasClean),
          );
          return;
        }

        const err = buildCloseError(e.code, e.reason, e.wasClean);
        if (err) this.emit("error", err);
      };
    });
  }

  private handleServerMessage(msg: ServerMessage): void {
    switch (msg.type) {
      case "prediction": {
        // Prefer the server's display_class (e.g. low-conf class-2 relabelled
        // to class-1). Falls back to raw `class` for older servers.
        const cls = msg.display_class ?? msg.class ?? 0;
        const conf = msg.confidence ?? 0;
        if (!this.warmedUp && conf > 0) {
          this.warmedUp = true;
          this.emit("warmupComplete");
        }
        this.emit("prediction", {
          cls,
          confidence: conf,
          source: msg.source,
          numFaces: msg.num_faces,
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
        // Push the current threshold now that the server is ready to receive it.
        this.sendControl({ action: "set_threshold", value: this.threshold });
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
        this.emit("error", {
          title: "Connection Stalled",
          message: "No pong received within timeout window.",
          detail: `${((performance.now() - this.lastPongAt) / 1000).toFixed(1)}s since last pong`,
        });
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
    };
  if (code === 1013)
    return {
      title: "Rate Limited",
      message: "Throttled by server — try again shortly.",
      detail: reason || `close code ${code}`,
      code,
    };
  if (code === 1006)
    return {
      title: "Connection Failed",
      message: "Could not reach the server.",
      detail: `The server may be down or unreachable. (close code ${code})`,
      code,
    };
  if (!wasClean)
    return {
      title: "Disconnected",
      message: "Connection lost unexpectedly.",
      detail: `code=${code} reason=${reason || "none"}`,
      code,
    };
  return null;
}
