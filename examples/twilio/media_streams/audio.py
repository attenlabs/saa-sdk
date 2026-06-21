"""Pure-NumPy G.711 µ-law codec + 8/16 kHz resampling for Twilio Media Streams.

Twilio sends inbound call audio as base64-encoded G.711 µ-law @ 8 kHz, and
accepts the same format on the outbound playback path. SAA expects PCM16
little-endian @ 16 kHz, framed in 100 ms blocks. This module bridges the
two without depending on ``audioop`` (removed in Python 3.13) or scipy.

The codec uses pre-built lookup tables: a 256-entry decoder and a
65,536-entry encoder. Both directions reduce to a single NumPy index op,
which is comfortably faster than the audioop C path on Twilio's volume
(~50 frames/s/call).

References:
- ITU-T G.711:           https://www.itu.int/rec/T-REC-G.711
- Sun / SoX reference C:  st_linear_to_ulaw / st_ulaw_to_linear
"""
from __future__ import annotations

import base64
from typing import Iterable

import numpy as np


# ── G.711 µ-law constants ────────────────────────────────────────────────

_SCALED_BIAS = 0x84   # 132, bias in the 16-bit-scaled domain
_ENCODE_BIAS = 0x21   # 33 , bias in the 14-bit-scaled domain
_ENCODE_CLIP = 0x1FFF
_SEG_END = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)


def _build_ulaw_decode_table() -> np.ndarray:
    table = np.empty(256, dtype=np.int16)
    for u in range(256):
        u_val = (~u) & 0xFF
        t = ((u_val & 0x0F) << 3) + _SCALED_BIAS
        t <<= (u_val & 0x70) >> 4
        sample = (_SCALED_BIAS - t) if (u_val & 0x80) else (t - _SCALED_BIAS)
        table[u] = sample
    return table


def _build_ulaw_encode_table() -> np.ndarray:
    table = np.empty(65536, dtype=np.uint8)
    for s in range(-32768, 32768):
        v = s >> 2  # 16-bit → 14-bit, sign-preserving
        if v < 0:
            v = -v
            mask = 0x7F
        else:
            mask = 0xFF
        if v > _ENCODE_CLIP:
            v = _ENCODE_CLIP
        v += _ENCODE_BIAS
        seg = 0
        while seg < 8 and v > _SEG_END[seg]:
            seg += 1
        if seg >= 8:
            uval = 0x7F ^ mask
        else:
            uval = ((seg << 4) | ((v >> (seg + 1)) & 0x0F)) ^ mask
        table[s & 0xFFFF] = uval & 0xFF
    return table


_ULAW_DECODE = _build_ulaw_decode_table()
_ULAW_ENCODE = _build_ulaw_encode_table()


# ── Codec ────────────────────────────────────────────────────────────────


def ulaw_to_pcm16(ulaw_bytes: bytes) -> np.ndarray:
    """Decode G.711 µ-law bytes to a PCM16 ``np.ndarray`` (dtype=int16)."""
    if not ulaw_bytes:
        return np.zeros(0, dtype=np.int16)
    return _ULAW_DECODE[np.frombuffer(ulaw_bytes, dtype=np.uint8)]


def pcm16_to_ulaw(pcm16: np.ndarray) -> bytes:
    """Encode a PCM16 ``np.ndarray`` (dtype=int16) to G.711 µ-law bytes."""
    if pcm16.size == 0:
        return b""
    if pcm16.dtype != np.int16:
        pcm16 = pcm16.astype(np.int16, copy=False)
    idx = pcm16.astype(np.int32) & 0xFFFF
    return _ULAW_ENCODE[idx].tobytes()


# ── Resampling ───────────────────────────────────────────────────────────


def upsample_8k_to_16k(pcm16_8k: np.ndarray) -> np.ndarray:
    """Linear-interpolation upsample of int16 PCM 8 kHz → 16 kHz.

    Quality is more than adequate for STT and the SAA classifier; both are
    insensitive to the small spectral image linear interpolation leaves
    above 4 kHz. For human-listening fidelity, replace with a polyphase
    FIR via ``scipy.signal.resample_poly``.
    """
    n = pcm16_8k.size
    if n == 0:
        return np.zeros(0, dtype=np.int16)
    out = np.empty(2 * n, dtype=np.int16)
    out[0::2] = pcm16_8k
    if n >= 2:
        avg = (pcm16_8k[:-1].astype(np.int32) + pcm16_8k[1:].astype(np.int32)) // 2
        out[1:-1:2] = avg.astype(np.int16)
        out[-1] = pcm16_8k[-1]
    else:
        out[1] = pcm16_8k[0]
    return out


def downsample_16k_to_8k(pcm16_16k: np.ndarray) -> np.ndarray:
    """Pair-averaging downsample of int16 PCM 16 kHz → 8 kHz.

    Pair averaging is a first-order box filter; it removes most aliasing
    above the 4 kHz Nyquist of the 8 kHz output. Twilio's PSTN carrier
    already band-limits to ~3.4 kHz, so the residual aliasing is below
    the carrier passband and inaudible. For higher quality, use a
    polyphase FIR.
    """
    n = pcm16_16k.size
    if n < 2:
        return pcm16_16k.astype(np.int16, copy=True)
    if n % 2:
        pcm16_16k = pcm16_16k[:-1]
    pairs = pcm16_16k.astype(np.int32).reshape(-1, 2)
    return ((pairs[:, 0] + pairs[:, 1]) // 2).astype(np.int16)


# ── Twilio convenience ───────────────────────────────────────────────────


def twilio_payload_to_pcm16_16k(b64_ulaw_payload: str) -> bytes:
    """Decode a Twilio inbound ``media.payload`` to PCM16 @ 16 kHz bytes."""
    pcm16_8k = ulaw_to_pcm16(base64.b64decode(b64_ulaw_payload))
    return upsample_8k_to_16k(pcm16_8k).tobytes()


def pcm16_16k_to_twilio_payload(pcm16_16k_bytes: bytes) -> str:
    """Encode PCM16 @ 16 kHz bytes to a Twilio outbound ``media.payload``."""
    pcm16_16k = np.frombuffer(pcm16_16k_bytes, dtype=np.int16)
    pcm16_8k = downsample_16k_to_8k(pcm16_16k)
    return base64.b64encode(pcm16_to_ulaw(pcm16_8k)).decode("ascii")


def chunk_ulaw_20ms(ulaw_bytes: bytes) -> Iterable[bytes]:
    """Yield 20 ms chunks (160 µ-law bytes) from a buffer of any length.

    Twilio's outbound playback path expects roughly 20 ms per ``media``
    event; longer chunks degrade barge-in latency. Trailing partial chunks
    are returned as-is (the receiver pads on its end).
    """
    step = 160
    for i in range(0, len(ulaw_bytes), step):
        yield ulaw_bytes[i:i + step]


__all__ = [
    "ulaw_to_pcm16",
    "pcm16_to_ulaw",
    "upsample_8k_to_16k",
    "downsample_16k_to_8k",
    "twilio_payload_to_pcm16_16k",
    "pcm16_16k_to_twilio_payload",
    "chunk_ulaw_20ms",
]
