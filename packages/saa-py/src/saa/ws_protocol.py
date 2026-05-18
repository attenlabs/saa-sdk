from __future__ import annotations

MSG_AUDIO = 0x01
MSG_VIDEO = 0x02


def frame_binary(tag: int, payload: bytes) -> bytes:
    return bytes([tag]) + payload
