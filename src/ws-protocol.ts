export const MSG_AUDIO = 0x01;
export const MSG_VIDEO = 0x02;

export type ServerMessage =
  | {
      type: "prediction";
      class: number | null;
      // Server's UI-preferred class (e.g. low-conf class-2 relabelled to
      // class-1). Falls back to `class` when absent.
      display_class?: number | null;
      confidence: number | null;
      source: string;
      num_faces: number;
    }
  | { type: "vad"; is_speech: boolean; probability: number }
  | { type: "state"; state: "listening" | "sending" | "cancelled" }
  | {
      type: "turn_ready";
      duration: number;
      audio_base64: string;
      frames?: Array<{ ts_offset_s: number; image_base64: string }>;
      /** Optional turn-context tag (e.g., "interjection_follow_up"). */
      context?: string;
    }
  | { type: "started" }
  | { type: "config"; model_class2_threshold: number }
  | { type: "interrupt"; fade_ms?: number; confidence?: number }
  | {
      type: "interjection";
      reason: string;
      audio_base64: string;
      duration_s: number;
    }
  | { type: "error"; message: string; detail?: string }
  | { type: "pong"; client_ts?: number; server_ts?: number };

export function base64ToInt16(b64: string): Int16Array {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
}

export function frameBinary(tag: number, payload: ArrayBufferLike): ArrayBuffer {
  const src = new Uint8Array(payload);
  const out = new Uint8Array(1 + src.byteLength);
  out[0] = tag;
  out.set(src, 1);
  return out.buffer;
}
