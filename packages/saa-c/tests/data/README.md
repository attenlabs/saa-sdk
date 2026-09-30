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

## The sample recording

`examples/demo/sample_drive_thru.wav` is the one real recording in the package.
The quickstart streams it with the demo, and `tests/live_smoke.py` streams it to
the hosted service and expects a `turn_ready`. It ships in a public repository,
so it must be owned outright by the project and redistributable under
Apache-2.0: record it in-house, with the speaker's written agreement to its
publication. Do not use text-to-speech output whose licence restricts
redistribution.

- Mono, 16 kHz, 16-bit PCM WAV.
- About 1 s of room tone, then 3 to 6 s of one person speaking to the device as
  a customer would (for example, a short drive-thru order), then 2 s of room
  tone.
- Normal speaking level, peaks around -6 dBFS, no clipping.

`tests/check_recording.py` checks a file against these points. Once the file is
committed, CTest runs the check too.

Recording it on Linux, with the microphone's card from `arecord -l`:

```bash
arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 9 sample_drive_thru.wav
```

From any other recording, convert with
`ffmpeg -i input.m4a -ac 1 -ar 16000 -sample_fmt s16 sample_drive_thru.wav`, and
trim it to the lengths above.
