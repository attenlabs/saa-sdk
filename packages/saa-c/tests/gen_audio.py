#!/usr/bin/env python3
"""Generate the synthetic WAV files the saa-c tests stream.

The mock server has no model, so test audio only needs the right shape: sample
rates, channel layouts, sample formats, and a speech-like on/off envelope. The
files are generated at test time and never committed. The one real speech clip,
used against the hosted service, is described in tests/data/README.md.

usage: gen_audio.py OUTDIR [--check]
"""
import argparse
import math
import os
import random
import struct
import sys
from array import array

SECONDS = 8.0
LEAD_S, TAIL_S = 1.0, 3.0          # silence before and after the "speech"
WORDS = 3                          # voiced segments between LEAD_S and SECONDS - TAIL_S
PEAK = 0.5                         # -6 dBFS
NOISE = 0.1                        # -20 dBFS on the non-speech channels

# name, sample rate, channels, sample format, speech on channel 0
FILES = [
    ("speech_16k_mono.wav",    16000, 1, "s16", True),
    ("speech_44k1_mono.wav",   44100, 1, "s16", True),
    ("speech_48k_stereo.wav",  48000, 2, "s16", True),
    ("speech_16k_6ch_f32.wav", 16000, 6, "f32", True),
    ("silence_16k_mono.wav",   16000, 1, "s16", False),
]


def voice(rate):
    """A 140 Hz buzz with harmonics and a syllable-rate envelope, gated into words."""
    n = int(rate * SECONDS)
    out = array("f", bytes(4 * n))
    span = SECONDS - LEAD_S - TAIL_S
    word = span / (WORDS + 0.25 * (WORDS - 1))
    gap = 0.25 * word
    for w in range(WORDS):
        start = LEAD_S + w * (word + gap)
        i0, i1 = int(start * rate), int((start + word) * rate)
        for i in range(i0, i1):
            t = i / rate
            u = (i - i0) / max(1, i1 - i0)
            env = 0.5 - 0.5 * math.cos(2 * math.pi * u)               # raised cosine per word
            env *= 0.6 + 0.4 * math.sin(2 * math.pi * 4.0 * t)        # ~4 syllables per second
            f0 = 140.0 * (1.0 + 0.02 * math.sin(2 * math.pi * 5.0 * t))
            s = sum(math.sin(2 * math.pi * h * f0 * t) / h for h in range(1, 9))
            out[i] = PEAK * env * s / 2.72                             # sum of 1/h for h = 1..8
    return out


def noise(n, seed):
    rng = random.Random(seed)
    return array("f", (rng.uniform(-NOISE, NOISE) for _ in range(n)))


def wav_bytes(rate, channels, fmt, chans):
    n = len(chans[0])
    if fmt == "s16":
        body = array("h", bytes(2 * n * channels))
        for c, samples in enumerate(chans):
            body[c::channels] = array("h", (max(-32768, min(32767, round(x * 32767))) for x in samples))
        tag, bits = 1, 16
    else:
        body = array("f", bytes(4 * n * channels))
        for c, samples in enumerate(chans):
            body[c::channels] = samples
        tag, bits = 3, 32
    if sys.byteorder == "big":
        body.byteswap()
    data = body.tobytes()
    block = channels * bits // 8
    fmt_chunk = struct.pack("<HHIIHH", tag, channels, rate, rate * block, block, bits)
    return (b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt_chunk) + 8 + len(data)) + b"WAVE" +
            b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk +
            b"data" + struct.pack("<I", len(data)) + data)


def check(path, rate, channels, fmt):
    with open(path, "rb") as f:
        raw = f.read()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE", "not RIFF/WAVE"
    tag, ch, sr, _, block, bits = struct.unpack("<HHIIHH", raw[20:36])
    assert (tag, ch, sr) == ((1 if fmt == "s16" else 3), channels, rate), (tag, ch, sr)
    assert raw[36:40] == b"data", "data chunk not where expected"
    size = struct.unpack("<I", raw[40:44])[0]
    assert size == len(raw) - 44 == int(rate * SECONDS) * block, "data size"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("outdir")
    ap.add_argument("--check", action="store_true", help="re-read every file and verify its header")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    voices = {}
    for i, (name, rate, channels, fmt, speech) in enumerate(FILES):
        n = int(rate * SECONDS)
        if speech:
            if rate not in voices:
                voices[rate] = voice(rate)
            first = voices[rate]
        else:
            first = array("f", bytes(4 * n))
        chans = [first] + [noise(n, seed=1000 * i + c) for c in range(1, channels)]
        path = os.path.join(args.outdir, name)
        with open(path, "wb") as f:
            f.write(wav_bytes(rate, channels, fmt, chans))
        if args.check:
            check(path, rate, channels, fmt)
        print(f"{path}: {rate} Hz, {channels} ch, {fmt}, {SECONDS:g} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
