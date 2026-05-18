import type { AudioCaptureOptions, VideoCaptureOptions } from "./types.js";
import { WORKLET_SOURCE } from "./worklet-source.js";

export const VIDEO_SEND_INTERVAL_MS = 250;
const VIDEO_BACKPRESSURE_BYTES = 1_000_000;
// Cap concurrent canvas.toBlob encodes. setInterval keeps firing even when a
// previous encode hasn't finished — on slow Windows machines a 1080p JPEG
// can take 200-400ms and the queue grows unboundedly, locking the main
// thread and starving the WS heartbeat. Drop frames instead.
const MAX_INFLIGHT_ENCODES = 2;

export interface AudioPipeline {
  close(): Promise<void>;
}

export async function createAudioPipeline(
  stream: MediaStream,
  workletUrlOverride: string | undefined,
  _opts: AudioCaptureOptions,
  onFrame: (pcm16: ArrayBuffer) => void,
): Promise<AudioPipeline> {
  const audioCtx = new AudioContext();

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
    onFrame(e.data as ArrayBuffer);
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
