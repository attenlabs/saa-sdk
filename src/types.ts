export interface VideoCaptureOptions {
  width?: number;
  height?: number;
  jpegQuality?: number;
}

export interface AudioCaptureOptions {
  targetSampleRate?: number;
}

export interface AttentionClientOptions {
  url?: string;
  token?: string;
  video?: VideoCaptureOptions;
  audio?: AudioCaptureOptions;
  workletUrl?: string;
  initialThreshold?: number;
}

export interface StartOptions {
  videoElement: HTMLVideoElement;
}

export interface PredictionEvent {
  cls: number;
  confidence: number;
  source: string;
  numFaces: number;
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
  /** Seconds from listening-start. Negative for pre-context frames. */
  tsOffsetS: number;
  imageBase64: string;
}

export interface TurnReadyEvent {
  audioBase64: string;
  audioPcm16: Int16Array;
  durationSec: number;
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
}

export interface DisconnectedEvent {
  code: number;
  reason: string;
  wasClean: boolean;
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
  interrupt: InterruptEvent;
  interjection: InterjectionEvent;
};

export type AttentionEventName = keyof AttentionEventMap;

export type AttentionListener<E extends AttentionEventName> =
  AttentionEventMap[E] extends void
    ? () => void
    : (payload: AttentionEventMap[E]) => void;
