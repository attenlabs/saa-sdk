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
  /**
   * Opt this session into utterance handling: the server segments speech into
   * utterances, transcribes each one and scores whether it was aimed at the
   * device, delivered as `utteranceEnded` events. Off by default. Feed the
   * assistant's spoken lines back with `addAssistantTurn()`; the classifier is
   * conditioned on that history.
   */
  utteranceHandling?: boolean;
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

/**
 * One finished utterance from the utterance pipeline (opt-in via
 * `utteranceHandling`). `prediction` is 1 (aimed at a person) or 2 (aimed at
 * the device); `decision` applies the server's one-sided threshold (see
 * `utteranceConfig.class1Threshold`). While the feature is in preview,
 * `preview` is true and the verdict is preview grade.
 */
export interface UtteranceEndedEvent {
  seq: number;
  text: string;
  /** null when the classifier failed open (`reason: "classifier_error"`). */
  prediction: 1 | 2 | null;
  confidence: number | null;
  decision: "respond" | "not_respond";
  reason: "scored" | "classifier_error" | string;
  /** Session clock, seconds. */
  startS: number;
  endS: number;
  /** The utterance hit the server's length cap; the text is a cut window. */
  truncated: boolean;
  /** 0 means no assistant turns were fed back; the verdict is unreliable then. */
  assistantTurns: number;
  preview: boolean;
  /** Server-side latency from the end of speech to this event, ms. */
  latencyMs: number | null;
  /** Present unless the server was configured without utterance audio. */
  audioBase64: string | null;
  audioPcm16: Int16Array | null;
}

export interface UtteranceConfigEvent {
  enabled: boolean;
  class1Threshold: number;
  preview: boolean;
  /** Set when `enabled` is false (mode_off, no_classifier, no_transcriber, unsupported). */
  reason: string | null;
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
  utteranceEnded: UtteranceEndedEvent;
  utteranceConfig: UtteranceConfigEvent;
};

export type AttentionEventName = keyof AttentionEventMap;

export type AttentionListener<E extends AttentionEventName> =
  AttentionEventMap[E] extends void
    ? () => void
    : (payload: AttentionEventMap[E]) => void;
