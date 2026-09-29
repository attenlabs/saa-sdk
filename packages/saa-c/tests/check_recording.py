#!/usr/bin/env python3
"""check_recording.py - checks the sample recording against its spec in
tests/data/README.md: a 16 kHz mono 16-bit PCM WAV with about 1 s of room tone,
3 to 6 s of speech, and 2 s of room tone, peaking near -6 dBFS without clipping.

usage: check_recording.py [WAV]    (default: examples/demo/sample_drive_thru.wav)
Exits 0 when every check passes.
"""
import math
import os
import struct
import sys
import wave

DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "examples", "demo", "sample_drive_thru.wav")


def dbfs(window):
    if not window:
        return -120.0
    rms = math.sqrt(sum(s * s for s in window) / len(window))
    return 20 * math.log10(rms / 32768.0) if rms > 0 else -120.0


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    try:
        with wave.open(path, "rb") as w:
            ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            frames = w.readframes(n)
    except (OSError, wave.Error) as e:
        print(f"{path}: {e}")
        return 1
    if width != 2:
        print(f"{path}: {8 * width}-bit samples; the sample must be 16-bit PCM")
        return 1
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)[::ch]      # channel 0
    dur = n / float(rate)
    win = rate // 10                                                      # 100 ms windows
    levels = [dbfs(samples[i:i + win]) for i in range(0, len(samples) - win + 1, win)]
    if not levels:
        print(f"{path}: too short")
        return 1
    loud = max(levels)
    speech = [i for i, level in enumerate(levels) if level > loud - 20.0]
    lead, tail = speech[0] * 0.1, dur - (speech[-1] + 1) * 0.1
    spoken = dur - lead - tail
    room = sorted(levels[:max(1, int(lead * 10) - 1)])
    room_db = room[len(room) // 2]
    peak = max(abs(s) for s in samples)
    peak_db = 20 * math.log10(peak / 32768.0) if peak else -120.0
    clipped = sum(1 for s in samples if s >= 32767 or s <= -32768)

    checks = [
        ((ch, rate) == (1, 16000), f"format: {rate} Hz, {ch} channel{'' if ch == 1 else 's'} (want 16000 Hz mono)"),
        (6.0 <= dur <= 10.0, f"length: {dur:.1f} s (want about 6 to 9 s)"),
        (lead >= 0.7, f"room tone before the speech: {lead:.1f} s (want about 1 s)"),
        (tail >= 1.5, f"room tone after the speech: {tail:.1f} s (want about 2 s)"),
        (2.5 <= spoken <= 6.5, f"speech: {spoken:.1f} s (want 3 to 6 s)"),
        (-12.0 <= peak_db <= -1.0, f"peak: {peak_db:.1f} dBFS (want around -6)"),
        (clipped == 0, f"clipped samples: {clipped} (want none)"),
        (room_db <= loud - 25.0, f"room tone at {room_db:.0f} dBFS against speech at {loud:.0f} (want 25 dB quieter)"),
    ]
    failed = [msg for ok, msg in checks if not ok]
    for ok, msg in checks:
        print(f"{'ok  ' if ok else 'FAIL'} {msg}")
    print(f"{path}: {'meets the spec' if not failed else 'does not meet the spec'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
