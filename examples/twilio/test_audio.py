"""Unit tests for the pure-NumPy µ-law codec + resamplers.

Run directly (no pytest needed)::

    python3 examples/twilio/test_audio.py

The SAA Python SDK already pulls in NumPy, so this is the only optional
test file in examples/twilio that requires a runtime dep.
"""
from __future__ import annotations

import base64
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    import numpy as np
except ImportError:
    print("skipped: numpy not installed")
    sys.exit(0)

import audio  # noqa: E402  (sys.path manipulation above)


failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"✓ {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"✗ {label}, {detail}")


# ── decode round-trips ──────────────────────────────────────────────────

zero_byte = bytes([0xFF])  # G.711 µ-law positive zero
check(
    "ulaw_to_pcm16: zero byte → 0",
    int(audio.ulaw_to_pcm16(zero_byte)[0]) == 0,
    detail=str(int(audio.ulaw_to_pcm16(zero_byte)[0])),
)

max_pos = bytes([0x80])  # loudest positive
check(
    "ulaw_to_pcm16: 0x80 → near +32k",
    int(audio.ulaw_to_pcm16(max_pos)[0]) > 30000,
    detail=str(int(audio.ulaw_to_pcm16(max_pos)[0])),
)

max_neg = bytes([0x00])  # loudest negative
check(
    "ulaw_to_pcm16: 0x00 → near -32k",
    int(audio.ulaw_to_pcm16(max_neg)[0]) < -30000,
    detail=str(int(audio.ulaw_to_pcm16(max_neg)[0])),
)


# ── encode → decode → encode is a fixed point ────────────────────────────

# G.711 has two zero codes (0x7F = negative zero, 0xFF = positive zero); both
# decode to 0, and any reasonable encoder collapses the input 0 onto a single
# code. We treat 0x7F↔0xFF as a permitted equivalence, same as the SoX /
# Sun Microsystems reference encoders.
all_bytes = bytes(range(256))
pcm = audio.ulaw_to_pcm16(all_bytes)
back = audio.pcm16_to_ulaw(pcm)
zero_codes = {0x7F, 0xFF}
mismatches = [
    i for i, (a, b) in enumerate(zip(back, all_bytes))
    if a != b and not ({a, b} <= zero_codes)
]
check(
    "encode(decode(b)) == b for every µ-law byte (modulo the dual-zero G.711 quirk)",
    not mismatches,
    detail=f"diverged at {mismatches[:5]}" if mismatches else "",
)


# ── encode zero ──────────────────────────────────────────────────────────

zero_pcm = np.zeros(1, dtype=np.int16)
check(
    "pcm16_to_ulaw(0) == 0xFF",
    audio.pcm16_to_ulaw(zero_pcm) == bytes([0xFF]),
    detail=hex(audio.pcm16_to_ulaw(zero_pcm)[0]) if audio.pcm16_to_ulaw(zero_pcm) else "",
)


# ── upsample 8 kHz → 16 kHz ──────────────────────────────────────────────

src = np.array([0, 100, 200, 300, 400], dtype=np.int16)
up = audio.upsample_8k_to_16k(src)
check("upsample doubles length", up.size == 2 * src.size, detail=f"got {up.size}")
check("upsample preserves source samples", bool(np.all(up[0::2] == src)))
# Interpolated samples should sit between neighbours.
between = np.all((up[1:-1:2] >= np.minimum(src[:-1], src[1:])) & (up[1:-1:2] <= np.maximum(src[:-1], src[1:])))
check("upsample interpolates monotonically", bool(between))


# ── downsample 16 kHz → 8 kHz ────────────────────────────────────────────

down_src = np.array([100, 200, 300, 400, 500, 600], dtype=np.int16)
down = audio.downsample_16k_to_8k(down_src)
check(
    "downsample halves length",
    down.size == 3,
    detail=f"got {down.size}",
)
check(
    "downsample averages pairs",
    list(down) == [150, 350, 550],
    detail=str(list(down)),
)


# ── upsample → downsample is near-lossless on smooth signals ─────────────

# Linear upsample + pair-average downsample is a [3,1]/4 boxcar with a
# fractional-sample group delay. On a single tone the residual is a small
# phase-shifted copy of the input. We assert it stays under 7% RMS, well
# below the noise floor of any reasonable PSTN STT pipeline.
t = np.arange(800, dtype=np.float64)
tone = (10000 * np.sin(2 * np.pi * 300 * t / 8000)).astype(np.int16)  # 300 Hz @ 8 kHz
round_trip = audio.downsample_16k_to_8k(audio.upsample_8k_to_16k(tone))
rms = float(np.sqrt(np.mean((tone.astype(np.float64) - round_trip.astype(np.float64)) ** 2)))
tone_amp = float(np.sqrt(np.mean(tone.astype(np.float64) ** 2)))
check(
    "upsample→downsample preserves a 300 Hz tone (RMS error < 7% of signal)",
    rms < 0.07 * tone_amp,
    detail=f"RMS error {rms:.1f} on signal RMS {tone_amp:.1f}",
)


# ── Twilio convenience round-trip ────────────────────────────────────────

tw_payload = base64.b64encode(bytes(range(256))).decode("ascii")
pcm_bytes = audio.twilio_payload_to_pcm16_16k(tw_payload)
check(
    "twilio_payload_to_pcm16_16k size matches 16 kHz expansion",
    len(pcm_bytes) == 256 * 2 * 2,  # 256 samples × 2 (pcm16) × 2 (upsample)
    detail=f"got {len(pcm_bytes)}",
)
out_payload = audio.pcm16_16k_to_twilio_payload(pcm_bytes)
check(
    "pcm16_16k_to_twilio_payload returns base64 ASCII",
    isinstance(out_payload, str) and out_payload.isascii() and len(out_payload) > 0,
    detail=f"len={len(out_payload)}",
)
rt = base64.b64decode(out_payload)
check(
    "pcm16_16k_to_twilio_payload mu-law length is 1/4 of input PCM16-16k",
    len(rt) == len(pcm_bytes) // 4,
    detail=f"len(mulaw)={len(rt)}, len(pcm)={len(pcm_bytes)}",
)


# ── 20 ms outbound chunking ─────────────────────────────────────────────

big = b"\x00" * 800  # 5 × 160 µ-law bytes
chunks = list(audio.chunk_ulaw_20ms(big))
check(
    "chunk_ulaw_20ms emits 5 × 160-byte chunks",
    len(chunks) == 5 and all(len(c) == 160 for c in chunks),
    detail=f"sizes={[len(c) for c in chunks]}",
)


if failures:
    print(f"\n{len(failures)} failure(s)", file=sys.stderr)
    sys.exit(1)
print("\nall audio codec checks passed")
