// canonical parser for the SAA binary turn payload (topic "saa" byte stream)
// layout (little-endian):
//   [u32 pcm_byte_len][pcm_byte_len bytes: int16 PCM @ 16 kHz mono]
//   [per frame: f32 ts_offset_s, u32 jpeg_byte_len, jpeg_byte_len bytes]
// JS counterpart to saa_livekit_client._wire.parse_turn_payload
// (pcm16 returned as an Int16Array here; the Python parser returns raw bytes)
export function parseTurnPayload(buf) {
  const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  let o = 0;

  const pcmLen = view.getUint32(o, true);
  o += 4;
  if (pcmLen % 2 !== 0) throw new Error("pcm byte length must be even (int16 PCM)");
  // copy out so the Int16Array view is 2-byte aligned regardless of buf offset
  const pcmBytes = buf.slice(o, o + pcmLen);
  o += pcmLen;
  const pcm16 = new Int16Array(pcmBytes.buffer);

  const frames = [];
  while (o + 8 <= buf.byteLength) {
    const tsOffsetS = view.getFloat32(o, true);
    o += 4;
    const jpegLen = view.getUint32(o, true);
    o += 4;
    const jpeg = buf.slice(o, o + jpegLen);
    o += jpegLen;
    frames.push({ tsOffsetS, jpeg });
  }

  return { pcm16, frames };
}
