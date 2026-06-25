export interface VideoCaptureOptions {
  width?: number;
  height?: number;
  jpegQuality?: number;
}

export interface AudioCaptureOptions {
  targetSampleRate?: number;
  onAudioFrame?: (pcm16: ArrayBuffer) => void;
  onWorkletError?: (err: unknown) => void;
  onContextStateChange?: (state: string) => void;
}

export interface AttentionClientOptions {
  url?: string;
  token?: string;
  video?: VideoCaptureOptions;
  audio?: AudioCaptureOptions;
  workletUrl?: string;
  initialThreshold?: number;
  enableAudio?: boolean;
  enableVideo?: boolean;
  serverProfile?: string;
  /** Auto-reconnect with backoff after an unclean mid-session drop (default true) */
  autoReconnect?: boolean;
}

export interface StartOptions {
  /** Required when video capture is enabled; omit for audio-only / feedVideo() mode. */
  videoElement?: HTMLVideoElement;
  /** Use this stream instead of getUserMedia; the SDK won't stop its tracks. */
  mediaStream?: MediaStream;
}

export interface PredictionEvent {
  cls: number;
  rawCls: number | null;
  confidence: number;
  source: string;
  numFaces: number;
  responding: boolean;
}

export interface VadEvent {
  probability: number;
  isSpeech: boolean;
}

export type ConversationState = "listening" | "sending" | "cancelled" | "idle";

export interface StateEvent {
  state: ConversationState;
}

export interface TurnFrame {
  tsOffsetS: number;
  imageBase64: string;
}

export interface TurnReadyEvent {
  audioBase64: string;
  audioPcm16: Int16Array;
  durationSec: number;
  serverTurnReadyTsMs: number | null;
  frames: TurnFrame[];
  context: string | null;
}

export interface ConfigEvent {
  modelClass2Threshold: number;
}

export interface InterruptEvent {
  fadeMs: number;
  confidence: number;
}

export interface InterjectionEvent {
  reason: string;
  audioBase64: string;
  audioPcm16: Int16Array;
  durationSec: number;
}

export interface StatsEvent {
  rttMs: number | null;
  bufferedAmount: number;
  sentVideo: number;
  skippedVideo: number;
  sentAudio: number;
  uptimeMs: number;
}

export interface AttentionErrorEvent {
  title: string;
  message: string;
  detail: string | null;
  code?: number;
  /** transport | auth | rate_limit | audio | server | environment */
  kind?: string;
  retriable?: boolean;
}

export interface DisconnectedEvent {
  code: number;
  reason: string;
  wasClean: boolean;
}

export interface ReconnectingEvent {
  attempt: number;
  delaySec: number;
  lastCode: number;
}

export interface ReconnectedEvent {
  attempts: number;
}

export type AttentionEventMap = {
  connected: void;
  started: void;
  warmupComplete: void;
  prediction: PredictionEvent;
  vad: VadEvent;
  state: StateEvent;
  turnReady: TurnReadyEvent;
  config: ConfigEvent;
  stats: StatsEvent;
  error: AttentionErrorEvent;
  disconnected: DisconnectedEvent;
  reconnecting: ReconnectingEvent;
  reconnected: ReconnectedEvent;
  interrupt: InterruptEvent;
  interjection: InterjectionEvent;
};

export type AttentionEventName = keyof AttentionEventMap;

export type AttentionListener<E extends AttentionEventName> =
  AttentionEventMap[E] extends void
    ? () => void
    : (payload: AttentionEventMap[E]) => void;
