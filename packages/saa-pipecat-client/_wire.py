"""Binary turn payload codec (parser side).

Wire format (`application/x-saa-turn`), little-endian throughout:

    [4-byte uint32: pcm_byte_len]
    [pcm_byte_len bytes: int16 LE PCM @ 16 kHz mono]
    [for each frame:
        [4-byte float32: ts_offset_s]
        [4-byte uint32:  jpeg_byte_len]
        [jpeg_byte_len bytes: raw JPEG]
    ]
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .types import TurnFrame


_HEADER = struct.Struct("<I")              # pcm_byte_len
_FRAME_HEADER = struct.Struct("<fI")       # ts_offset_s, jpeg_byte_len


class TurnPayloadError(ValueError):
    """Raised when the binary buffer does not match the wire format."""


@dataclass(frozen=True)
class ParsedTurnPayload:
    pcm16: bytes
    frames: list[TurnFrame]


def parse_turn_payload(buf: bytes) -> ParsedTurnPayload:
    """Decode a turn payload received via LiveKit byte stream.

    Raises TurnPayloadError on truncation or invalid length fields.
    """
    if len(buf) < _HEADER.size:
        raise TurnPayloadError(
            f"buffer too short for header: {len(buf)} < {_HEADER.size}"
        )

    (pcm_len,) = _HEADER.unpack_from(buf, 0)
    pcm_start = _HEADER.size
    pcm_end = pcm_start + pcm_len
    if pcm_end > len(buf):
        raise TurnPayloadError(
            f"pcm length {pcm_len} exceeds buffer ({len(buf) - pcm_start} remaining)"
        )

    pcm16 = bytes(buf[pcm_start:pcm_end])

    frames: list[TurnFrame] = []
    cursor = pcm_end
    while cursor < len(buf):
        if cursor + _FRAME_HEADER.size > len(buf):
            raise TurnPayloadError(
                f"truncated frame header at offset {cursor} "
                f"({len(buf) - cursor} bytes remaining, need {_FRAME_HEADER.size})"
            )
        ts_offset_s, jpeg_len = _FRAME_HEADER.unpack_from(buf, cursor)
        cursor += _FRAME_HEADER.size
        if cursor + jpeg_len > len(buf):
            raise TurnPayloadError(
                f"truncated frame JPEG at offset {cursor} "
                f"(need {jpeg_len}, have {len(buf) - cursor})"
            )
        frames.append(TurnFrame(
            ts_offset_s=float(ts_offset_s),
            jpeg_bytes=bytes(buf[cursor:cursor + jpeg_len]),
        ))
        cursor += jpeg_len

    return ParsedTurnPayload(pcm16=pcm16, frames=frames)


def encode_turn_payload(pcm16: bytes, frames: list[TurnFrame]) -> bytes:
    parts: list[bytes] = [_HEADER.pack(len(pcm16)), pcm16]
    for f in frames:
        parts.append(_FRAME_HEADER.pack(f.ts_offset_s, len(f.jpeg_bytes)))
        parts.append(f.jpeg_bytes)
    return b"".join(parts)
