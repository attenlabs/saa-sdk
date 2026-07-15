import type { AudioCaptureOptions, VideoCaptureOptions } from "./types.js";
import { WORKLET_SOURCE } from "./worklet-source.js";

export const VIDEO_SEND_INTERVAL_MS = 250;
const VIDEO_BACKPRESSURE_BYTES = 1_000_000;
// Cap concurrent canvas.toBlob encodes. setInterval keeps firing even when a
// previous encode hasn't finished — on slow Windows machines a 1080p JPEG
// can take 200-400ms and the queue grows unboundedly, locking the main
// thread and starving the WS heartbeat. Drop frames instead.
const MAX_INFLIGHT_ENCODES = 2;

// wire audio format — keep in lock-step with the worklet (audio-processor.js)
export const TARGET_SAMPLE_RATE = 16000;
export const SEND_INTERVAL_SAMPLES = 1600; // 100 ms at 16 kHz

/** Normalize fed audio (Float32 / Int16 / raw int16 buffer) to float32 mono [-1, 1]. */
export function toFloat32Mono(
  audio: Int16Array | Float32Array | ArrayBuffer | ArrayBufferView,
): Float32Array {
  if (audio instanceof Float32Array) return audio;
  if (audio instanceof Int16Array) {
    const out = new Float32Array(audio.length);
    for (let i = 0; i < audio.length; i++) out[i] = audio[i]! / 32768;
    return out;
  }
  // raw buffer/view → little-endian int16 PCM
  const i16 =
    audio instanceof ArrayBuffer
      ? new Int16Array(audio)
      : new Int16Array(audio.buffer, audio.byteOffset, Math.floor(audio.byteLength / 2));
  const out = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) out[i] = i16[i]! / 32768;
  return out;
}

/** Linear-interpolation resample — matches the worklet's downsampler; also upsamples. */
export function resampleLinear(
  samples: Float32Array,
  fromRate: number,
  toRate: number,
): Float32Array {
  if (fromRate === toRate || samples.length === 0) return samples;
  const ratio = fromRate / toRate;
  const outLen = Math.floor(samples.length / ratio);
  if (outLen === 0) return new Float32Array(0);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const srcIdx = i * ratio;
    const lo = Math.floor(srcIdx);
    const hi = Math.min(lo + 1, samples.length - 1);
    const frac = srcIdx - lo;
    out[i] = samples[lo]! * (1 - frac) + samples[hi]! * frac;
  }
  return out;
}

/** Float32 [-1,1] → Int16 PCM (clamped), matching the worklet's quantization. */
export function floatToPcm16(chunk: Float32Array): Int16Array {
  const pcm16 = new Int16Array(chunk.length);
  for (let i = 0; i < chunk.length; i++) {
    const s = Math.max(-1, Math.min(1, chunk[i]!));
    pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return pcm16;
}

export interface AudioPipeline {
  close(): Promise<void>;
}

export async function createAudioPipeline(
  stream: MediaStream,
  workletUrlOverride: string | undefined,
  opts: AudioCaptureOptions,
  onFrame: (pcm16: ArrayBuffer) => void,
): Promise<AudioPipeline> {
  const audioCtx = new AudioContext();

  // Everything past the constructor can throw — addModule rejects when the
  // blob: worklet is CSP-blocked or an ad-blocker/bad workletUrl intercepts
  // it, and createMediaStreamSource / AudioWorkletNode can throw too. The
  // context is never returned on a throw, so no caller can close it; without
  // this catch each failed start() strands a live AudioContext and browsers
  // hard-cap concurrent contexts, turning a retriable error into a page reload.
  try {
    let blobUrl: string | null = null;
    const url = workletUrlOverride ?? (() => {
      const blob = new Blob([WORKLET_SOURCE], { type: "application/javascript" });
      blobUrl = URL.createObjectURL(blob);
      return blobUrl;
    })();

    try {
      await audioCtx.audioWorklet.addModule(url);
    } finally {
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    }

    const source = audioCtx.createMediaStreamSource(stream);
    const workletNode = new AudioWorkletNode(audioCtx, "pcm-capture");
    workletNode.port.onmessage = (e: MessageEvent) => {
      const pcm16 = e.data as ArrayBuffer;
      onFrame(pcm16);
      opts.onAudioFrame?.(pcm16);
    };
    // The worklet runs on the audio thread; an unhandled throw can silently
    // kill stream with no main-thread signal, show so the caller
    // can log / show a toast / attempt restart.
    workletNode.onprocessorerror = (ev: Event) => {
      try {
        opts.onWorkletError?.(ev);
      } catch {}
    };
    // AudioContext can transition to "suspended" (autoplay, ios safari etc.)
    // Capture so the caller can surface it.
    audioCtx.onstatechange = () => {
      try {
        opts.onContextStateChange?.(audioCtx.state);
      } catch {}
    };
    source.connect(workletNode);

    return {
      async close() {
        try {
          workletNode.disconnect();
        } catch {}
        try {
          source.disconnect();
        } catch {}
        try {
          await audioCtx.close();
        } catch {}
      },
    };
  } catch (err) {
    try {
      await audioCtx.close();
    } catch {}
    throw err;
  }
}

export interface VideoPipeline {
  stop(): void;
}

export type SkipReason = "backpressure" | "blob" | "closed";

export function startVideoPipeline(
  video: HTMLVideoElement,
  opts: VideoCaptureOptions,
  getBufferedAmount: () => number,
  isOpen: () => boolean,
  onFrame: (jpeg: ArrayBuffer) => void,
  onSkipped: (reason: SkipReason) => void,
): VideoPipeline {
  const quality = opts.jpegQuality ?? 0.5;
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth || opts.width || 1920;
  canvas.height = video.videoHeight || opts.height || 1080;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("2D canvas context unavailable");

  let inflight = 0;

  const timer = setInterval(() => {
    if (!isOpen()) {
      onSkipped("closed");
      return;
    }
    if (inflight >= MAX_INFLIGHT_ENCODES) {
      onSkipped("backpressure");
      return;
    }
    if (getBufferedAmount() > VIDEO_BACKPRESSURE_BYTES) {
      onSkipped("backpressure");
      return;
    }
    inflight++;
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob(
      (blob) => {
        if (!blob) {
          inflight--;
          onSkipped("blob");
          return;
        }
        if (!isOpen()) {
          inflight--;
          onSkipped("closed");
          return;
        }
        blob
          .arrayBuffer()
          .then(onFrame)
          .catch(() => onSkipped("blob"))
          .finally(() => {
            inflight--;
          });
      },
      "image/jpeg",
      quality,
    );
  }, VIDEO_SEND_INTERVAL_MS);

  return {
    stop() {
      clearInterval(timer);
    },
  };
}
