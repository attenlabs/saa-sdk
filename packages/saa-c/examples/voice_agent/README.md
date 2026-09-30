# Voice agent

A voice agent on a device, in C. The microphone and the camera stream to SAA.
Each turn SAA hands back goes to the [OpenAI Realtime
API](https://platform.openai.com/docs/guides/realtime), and the reply plays from
the device's speaker. SAA's events drive the turn-taking, so the device answers
speech meant for it, lets side talk pass, and stops when someone interrupts it.

## What it does

- **A turn.** On `turn_ready`, the agent resamples the turn's audio from 16 kHz to
  the Realtime session's 24 kHz and appends it to the session's input buffer.
  Then it commits the buffer and asks for a reply. The turn's camera frames are
  not sent.
- **The reply** is collected whole, then played. Playback starts once the whole
  reply has arrived: 2.3 to 3.9 s after the turn on a Raspberry Pi 4, longer for
  longer replies.
- **While it plays,** the device is marked as responding
  (`saa_client_responding_start()`), and it stays marked for 300 ms after, while
  the room's echo dies down. What more it does about its own voice depends on
  the hardware; see [Echo handling](#echo-handling).
- **Barge-in.**
  - SAA sends `interrupt` when someone speaks to the device while it talks. The
    agent fades the reply out over the interrupt's `fade_ms`. It also cuts the
    model's copy of the reply (`conversation.item.truncate`) to what was
    actually played, so the model's history holds only what the user heard.
  - A new turn that arrives while the reply is still being generated cancels it
    (`response.cancel`), and none of it plays. One that arrives while the reply
    plays fades it out, as an interrupt does, unless barge-in is off (see [Echo
    handling](#echo-handling)).
- **Reconnects.**
  - SAA's client reconnects by itself.
  - The Realtime socket reconnects with backoff, 1 s doubling to 30 s, and the
    model's conversation history starts over with it. A turn that arrives while
    the socket is down is dropped, with a line saying so.
  - A Realtime session lasts at most 60 minutes, so the agent reconnects between
    turns once one is 50 minutes old.

## Build

saa-c's own build makes it, as `build/saa_voice_agent`:

```bash
sudo apt install libasound2-dev          # the ALSA speaker; without it, only file:
cmake -S . -B build -DSAA_WITH_CAPTURE=ON # capture: the live microphone and camera
cmake --build build
```

Without `SAA_WITH_CAPTURE` it still runs from a recording (`--wav`), and from a
camera on stdin (`--mjpeg -`). On macOS only the file speaker is built. To build
it from a checkout of saa-c against an installed library:

```bash
cmake -S examples/voice_agent -B build-va -DCMAKE_PREFIX_PATH=/path/to/prefix
cmake --build build-va
```

## Run

```bash
export SAA_API_KEY=... OPENAI_API_KEY=...

# a ReSpeaker Lite as microphone and speaker, and a USB camera
./build/saa_voice_agent --alsa hw:CARD=Lite --v4l2 /dev/video2 --speaker hw:CARD=Lite

# a recording in and the reply to a WAV file, on any machine
./build/saa_voice_agent --wav examples/demo/sample_drive_thru.wav --speaker file:reply.wav --duration 45
```

Both keys come only from the environment, and neither is ever logged. The agent
prints a line for each turn, like this one from a Raspberry Pi 4 on Wi-Fi:

```
   13.8  SAA warmed up: listening
   34.5  turn 1, 21.0 s: heard "Hey, can I get a cheeseburger with large fries and a Coke Zero?" | reply 8.8 s, played: "Got it, a cheeseburger with large fries and a Coke Zero. Do you want any sauces or toppings added or changed?" | created +0.77 s, audio +2.77 s, playing +2.78 s
```

- **21.0 s** is the turn's length, including SAA's pre-roll.
- **heard** is OpenAI's transcription of the turn, by `whisper-1`. It is a guide
  to what the model heard, not the model's own reading, and it can arrive late
  or not at all; the line waits 2 s for it.
- **reply** is the reply's length and what became of it:
  - played;
  - interrupted at 1.3 s;
  - cancelled before playback;
  - no audio: the reply failed, with the reason;
  - lost: the Realtime socket closed before the reply came;
  - dropped: the socket was down when the turn came.

  Then what the reply said.
- **The times** count from `turn_ready`: to OpenAI's `response.created`, to the
  reply's audio being complete, and to playback starting.

`--events FILE` also writes every event as a JSON line. `response_done` carries
each reply's token usage, and `agent_start` carries `mono_t0`, the start in
seconds of `CLOCK_MONOTONIC`.

| Option | |
|---|---|
| `--alsa DEV` | Capture a microphone (a library built with `SAA_WITH_CAPTURE`) |
| `--wav FILE` | A recording instead, paced in real time from SAA's warmup; `-` reads a WAV stream on stdin |
| `--channel N` | The channel to keep (0) |
| `--v4l2 DEV` | Capture an MJPEG camera, with `--alsa` |
| `--mjpeg -` | JPEG frames on stdin, such as `rpicam-vid -t 0 --codec mjpeg -o -` |
| `--size WxH`, `--fps N` | The camera's frame size (640x480) and rate (4) |
| `--speaker DEV` | An ALSA device (`default`), or `file:PATH` to write a WAV at real-time pace |
| `--gain-db DB` | Playback gain, -30 to +20 dB (+6) |
| `--tail-ms MS` | How long responding stays on after playback (300) |
| `--mute-while-talking` | Also mute SAA while the device talks, so what the microphone hears of it stays out of the next turn |
| `--barge-in on\|off` | Whether an interrupt stops the reply (on). Off: interrupts are ignored, and turns that arrive while the device talks are dropped |
| `--model NAME`, `--voice NAME` | `gpt-realtime-2` and `sage` |
| `--reasoning EFFORT` | The model's reasoning effort (`minimal`); `""` leaves it out |
| `--instructions TEXT` | The model's instructions, or `@FILE` |
| `--greet TEXT` | Instructions for a greeting once SAA warms up, such as "Greet the customer in one short sentence" |
| `--interjections` | Answer SAA's interjections, when people talked and then went quiet, with a brief check-in |
| `--utterance` | Utterance handling (preview); each reply goes to its assistant history |
| `--threshold F` | The class-2 threshold (0.7) |
| `--url URL`, `--openai-url URL` | Other endpoints; the model goes into the Realtime URL's query |
| `--ca FILE` | A CA bundle, for both services |
| `--events FILE` | Every event as a JSON line |
| `--record-turns DIR` | Each turn as `DIR/NNNN_turn.wav`, the 16 kHz audio SAA sent, and each reply as `NNNN_reply.wav`, the 24 kHz audio the model sent |
| `--duration S` | Stop after S seconds; SIGINT and SIGTERM stop it too |
| `--help`, `--version` | |

Exit codes:

| Code | Meaning |
|---|---|
| 0 | The run ended cleanly |
| 2 | A key was rejected |
| 3 | Rate limited, or no capacity |
| 4 | The network or TLS failed |
| 5 | Bad arguments |
| 6 | The microphone or the speaker did not open |

A camera that does not open leaves the session audio only, as in saa-c.

## Echo handling

The device must not answer its own voice. SAA knows when the device talks,
from `responding_start`, but it hears what the microphone hears. How much of
the device's voice that is depends on the hardware, so the handling is a
choice:

| The microphone hears the speaker | Flags | Barge-in |
|---|---|---|
| Barely: good echo cancelling, or headphones | (none) | yes |
| Some: echo cancelling that leaks a little | `--mute-while-talking` | yes; a strong leak can still cut a reply short |
| Clearly, or a leak strong enough to interrupt | `--mute-while-talking --barge-in off` | no: the device finishes each reply |

- **Responding alone** relies on the echo canceller: whatever it lets through,
  SAA may take for a person speaking to the device.
- **Muting** keeps what the microphone hears while the device talks out of an
  ordinary turn. SAA still hears it, though, and a strong enough leak sounds like
  someone speaking to the device: SAA then interrupts. An interrupt makes a turn
  of the audio before it, muted or not, since that is how a real barge-in keeps
  what the user said over the device. So a false interrupt still hands the model
  its own words.
- **No barge-in** ignores interrupts, so a leak cannot cut a reply short. SAA
  clears its responding flag when it interrupts, so the agent tells it again that
  the device is talking. The turn SAA makes from its interrupt, and any turn that
  arrives while the device talks, is the device's own voice, and is dropped with
  a line saying so. Speak after the device has finished.

How to tell which row a device is on: run it with `--record-turns DIR` and
`--events FILE`. When replies are cut short by `interrupt` lines nobody caused,
or a turn's `heard` text is the device's last reply, the microphone hears the
speaker. The raw class under SAA's `ai_responding` label is `raw_cls` in the
`prediction` events.

## The speaker

- **Its format.** The agent asks the device for the reply's own format, 24 kHz
  mono, with ALSA's resampling off. A device that refuses keeps its nearest rate
  and its own channel count. The reply is then converted to that rate, and the
  same signal goes to every channel. The ReSpeaker Lite plays only 16 kHz stereo,
  and the first line says so:
  `speaker: hw:CARD=Lite, 16000 Hz, 2 channels (replies converted from 24 kHz mono)`.
- **Echo cancelling.** On a ReSpeaker, play through the ReSpeaker itself. Its
  firmware uses what it plays as the echo canceller's reference, and takes the
  device's own voice out of its processed microphone signal. That is what lets
  someone interrupt the reply: the classifier hears the person, not the speaker.
  On boards with several channels, keep the processed one with `--channel`.
- **Without echo cancelling** (a separate speaker, HDMI, a laptop), the device's
  voice reaches SAA while it plays. SAA knows the device is responding, but a
  confident class-2 prediction then interrupts the reply. See [Echo
  handling](#echo-handling) for the flags; lowering `--gain-db`, or headphones,
  also help.
- **Echo cancelling can fade.** A canceller followed by automatic gain control
  can hold the device's voice down at first and then turn what is left back up.
  One ReSpeaker Lite with its speaker close by did this: 12 to 34 dB of
  cancelling for the first 3 s of a reply, then 0 to 10 dB. After playback its
  gain stayed high for a second or so, and amplified room noise that SAA could
  take for speech. A longer `--tail-ms` covers that.
- **PipeWire** (Raspberry Pi OS with a desktop) holds a sound card while any of
  its clients plays or records, and for about 5 s after. A `hw:` open then fails
  with "Device or resource busy"; the agent waits up to 6 s for a busy speaker
  when it starts. Close the program using the card, or play through `default`.
  `default` goes through PipeWire, and it reaches the ReSpeaker's echo reference
  only when PipeWire's output is the ReSpeaker.
- **Check it first:** `speaker-test -D hw:CARD=Lite -c 2 -r 16000 -t sine -l 1`.

## What a turn costs

- **Tokens.** OpenAI bills tokens, and `--events` logs them per reply. The run
  above used 243 input tokens, 210 of them audio, and 193 output tokens: 136
  audio, 57 text, and 19 of reasoning. Each reply reads the conversation so far,
  so input tokens grow over a session, part of them at the cached rate. OpenAI's
  pricing page turns tokens into money.
- **Time.** From `turn_ready` to the first sound, 2.3 to 3.9 s in these runs.
  Most of it is the model generating the whole reply, because playback waits for
  all of it; short instructions keep replies short.
- **The device.** Resampling costs about 2 ms a second of audio on a Raspberry
  Pi 4: 40 ms for a 20 s turn, and as much again to convert a reply for a
  16 kHz speaker.

## Troubleshooting

- **Exit 2:** "OpenAI refused the connection (HTTP 401)" means `OPENAI_API_KEY`;
  an SAA `error` line means `SAA_API_KEY`.
- **Exit 6:** the line before it names the device. "Device or resource busy"
  means another program has it; see PipeWire above. `aplay -L` and `arecord -L`
  list the names.
- **The device interrupts itself, or answers its own words:** the microphone hears
  the speaker; see [Echo handling](#echo-handling).
- **It answers something nobody said,** often heard as "Thanks for watching!" or
  as nothing: the microphone picked up noise just after a reply. Raise
  `--tail-ms`.
- **Nothing plays:** run `speaker-test`, check the `speaker:` line, and raise
  `--gain-db`.
- **"Realtime closed ... retrying":** the socket to OpenAI dropped. Turns that
  come while it is down are dropped, and the model's history starts over.
- **No turns:** see saa-c's own troubleshooting. The speech may have come before
  "SAA warmed up", or not have been addressed to the device.

## How it is built

- `main.c`: the flags, the wiring, and the turn state. Every event goes onto one
  queue, handled in order on the main thread, so the turn state has one owner.
- `realtime.c`: the Realtime client, over libwebsockets on a thread of its own.
  The key goes into the handshake's `Authorization` header, and never an
  `OpenAI-Beta` header.
- `playback.c`: the speaker, on a thread of its own. It stays open and writes
  silence between replies.
- `resample.c`: a windowed-sinc rate converter, used both ways.
- saa-c's callbacks only copy and post, as saa-c asks.

It uses only saa-c's public API. It compiles the vendored cJSON and base64 from
`third_party/` into itself, so the library archive's copies are never linked;
don't link it with `--whole-archive`. Creating the SAA client sets
libwebsockets' logging for the whole process, to errors and warnings, which
covers the Realtime socket too, so no header reaches a log. Create it before any
other libwebsockets context.

`ctest -R voice_agent` runs it in each echo mode, with no hardware and no keys,
against a mock of SAA and a mock of the Realtime API
(`tests/run_voice_agent.py`). It checks the audio sent, the responding and mute
calls and their timing, the interrupt, the truncate, the cancel, and what the
speaker played.

## License

Apache-2.0, as the rest of saa-c. See [LICENSE](../../LICENSE) and
[NOTICE](../../NOTICE).
