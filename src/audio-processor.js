/**
 * AudioWorklet processor that captures PCM audio and sends it to the main thread.
 *
 * If the AudioContext sample rate matches the target (16 kHz), samples pass through.
 * Otherwise, downsamples via linear interpolation.
 *
 * Output: Int16 PCM ArrayBuffer posted via port.postMessage (transferable).
 */

const TARGET_RATE = 16000;
const SEND_INTERVAL_SAMPLES = 1600; // 100ms at 16kHz

class PCMCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = new Float32Array(0);
    this._ratio = sampleRate / TARGET_RATE;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0] || input[0].length === 0) return true;

    const channelData = input[0];

    if (this._ratio === 1) {
      this._accumulate(channelData);
    } else {
      this._accumulate(this._downsample(channelData));
    }

    return true;
  }

  _downsample(samples) {
    const outputLen = Math.floor(samples.length / this._ratio);
    if (outputLen === 0) return new Float32Array(0);

    const out = new Float32Array(outputLen);
    for (let i = 0; i < outputLen; i++) {
      const srcIdx = i * this._ratio;
      const lo = Math.floor(srcIdx);
      const hi = Math.min(lo + 1, samples.length - 1);
      const frac = srcIdx - lo;
      out[i] = samples[lo] * (1 - frac) + samples[hi] * frac;
    }
    return out;
  }

  _accumulate(samples) {
    const combined = new Float32Array(this._buffer.length + samples.length);
    combined.set(this._buffer);
    combined.set(samples, this._buffer.length);
    this._buffer = combined;

    while (this._buffer.length >= SEND_INTERVAL_SAMPLES) {
      const chunk = this._buffer.slice(0, SEND_INTERVAL_SAMPLES);
      this._buffer = this._buffer.slice(SEND_INTERVAL_SAMPLES);

      const pcm16 = new Int16Array(chunk.length);
      for (let i = 0; i < chunk.length; i++) {
        const s = Math.max(-1, Math.min(1, chunk[i]));
        pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }

      this.port.postMessage(pcm16.buffer, [pcm16.buffer]);
    }
  }
}

registerProcessor("pcm-capture", PCMCaptureProcessor);
