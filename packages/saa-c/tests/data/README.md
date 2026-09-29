# Test audio

Tests against the mock server use synthetic audio from `tests/gen_audio.py`,
generated at test time (CTest runs it into the build tree). Nothing it produces
is committed.

| File | Rate | Channels | Format | Content |
|---|---|---|---|---|
| `speech_16k_mono.wav` | 16 kHz | 1 | int16 | 1 s silence, 4 s speech-like tone, 3 s silence |
| `speech_44k1_mono.wav` | 44.1 kHz | 1 | int16 | same, to exercise resampling |
| `speech_48k_stereo.wav` | 48 kHz | 2 | int16 | tone on channel 0, noise on channel 1 |
| `speech_16k_6ch_f32.wav` | 16 kHz | 6 | float32 | tone on channel 0, noise elsewhere (mic-array layout) |
| `silence_16k_mono.wav` | 16 kHz | 1 | int16 | digital silence |

## `live_smoke.wav`

The live smoke test streams one real recording to the hosted service and
expects a `turn_ready`. It is the only audio file committed here, so it must be
owned outright by the project and redistributable under Apache-2.0: record it
in-house. Do not use text-to-speech output whose licence restricts
redistribution.

- Mono, 16 kHz, 16-bit PCM WAV.
- About 1 s of room tone, then 3 to 6 s of one person speaking to the device as
  a customer would (for example, a short drive-thru order), then 2 s of room
  tone.
- Normal speaking level, peaks around -6 dBFS, no clipping.
