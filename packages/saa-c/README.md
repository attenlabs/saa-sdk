# saa-c

C client for [Attention Labs](https://attentionlabs.ai) selective auditory
attention (SAA), the addressee classifier. Stream a device's audio, and
optionally JPEG frames, to the hosted service. Back comes a prediction every
250 ms, plus a `turn_ready` event carrying the audio of each utterance meant for
the device, ready for your STT.

> **Status: pre-release.** The API may still change before 1.0.

- C99 core with a C ABI, so C++, .NET, Java, Go, and Rust can bind to it. A
  header-only C++11 wrapper, `saa/saa_client.hpp`, comes with it.
- Depends on libwebsockets (4.1 or later) and the TLS library it was built with.
- Targets Raspberry Pi OS (arm64 and armv7) and x86_64 Linux; builds on macOS
  for development.
- Two ways in. Feed mode: your code pushes the audio and JPEG frames it already
  has. Capture, on Linux: built with `SAA_WITH_CAPTURE`, the library opens an
  ALSA microphone and a V4L2 MJPEG camera itself.

## Sign up

Get your API key at [attentionlabs.ai](https://attentionlabs.ai). Keep it in
the environment as `SAA_API_KEY`; the library never logs it.

## Quickstart

### 1. Build

```bash
# Debian 12 or 13, Ubuntu 24.04, Raspberry Pi OS (bookworm or trixie)
sudo apt install build-essential cmake pkg-config libwebsockets-dev libssl-dev
# macOS
brew install libwebsockets

cmake -S . -B build
cmake --build build
```

Ubuntu 22.04's libwebsockets (4.0) is too old; build in a Debian 13 container there.

### 2. Stream the sample

```bash
export SAA_API_KEY=...
./build/saa_client_demo --wav examples/demo/sample_drive_thru.wav --wait-warmup
```

The demo prints every event as one JSON line.
- It connects, and gets `started`.
- `warmup_complete` arrives about 12.5 s later. With `--wait-warmup`, the demo
  streams silence until then.
- It then streams the recording, and a `turn_ready` line reports the utterance
  it heard: `samples` of 16 kHz audio lasting `duration_sec`.
- It stops after a few seconds of silence, and exits 0.

Prediction and VAD lines arrive four times a second; `grep -v -E '"(prediction|vad)"'`
hides them.

### 3. Use it from your code

```c
#include <saa/saa_client.h>

static void on_turn(void *ud, const saa_turn_ready_ev_t *ev)
{
    /* ev->audio_pcm16: ev->num_samples of 16 kHz mono audio for your STT */
}

saa_client_config_t cfg = {0};
cfg.token = getenv("SAA_API_KEY");
cfg.video_mode = SAA_VIDEO_NONE;              /* audio only */
cfg.callbacks.on_turn_ready = on_turn;

saa_client_t *c = saa_client_create(&cfg);
saa_client_start_wait(c, 15000);              /* 0 once the service has started */

/* from your audio callback: any rate, int16 or float, mono or interleaved */
saa_client_feed_audio_interleaved(c, buf, frames, 48000, SAA_AUDIO_S16, 2, 0);

/* around your own TTS playback, so the service knows the device is talking */
saa_client_responding_start(c);
saa_client_responding_stop(c);

saa_client_destroy(c);                        /* stops first */
```

[`examples/minimal/main.c`](examples/minimal/main.c) is the complete program.
Install the library, then build against it with pkg-config or CMake:

```bash
sudo cmake --install build          # or: cmake --install build --prefix ~/saa
cc main.c -o minimal $(pkg-config --cflags --libs saaclient)
```

```cmake
find_package(saaclient REQUIRED)    # add the prefix to CMAKE_PREFIX_PATH if needed
target_link_libraries(your_app PRIVATE saaclient::saaclient)
```

### 4. Add video

If the device has a camera, send it too: audio alone runs at a lower operating
point. Set `cfg.video_mode = SAA_VIDEO_FEED`, then pass complete JPEG frames to
`saa_client_feed_video()` about four times a second. 640×480 at quality 50 is
plenty, at about 20 KB a frame. With the demo, `--jpeg-dir DIR` sends `DIR/*.jpg`
at 4 fps.

### 5. Try a live microphone

The minimal example reads raw PCM on stdin, so on Linux a microphone can feed it
directly:

```bash
arecord -q -f S16_LE -r 16000 -c 1 -t raw | ./build/saa_minimal
```

It prints `ready` at `warmup_complete`, then one line per `turn_ready`. Find
the microphone with `arecord -l`, and pass `-D plughw:CARD,0` to pick it.

Or let the library open the devices itself. Build with capture (below), then:

```bash
./build/saa_client_demo --alsa default --v4l2 /dev/video0
```

### 6. From C++

`saa/saa_client.hpp` wraps the C API in `saa::Client`, with a `std::function`
per event:

```cpp
#include <saa/saa_client.hpp>

saa::Client::Config cfg;
cfg.token = std::getenv("SAA_API_KEY");
cfg.on_turn_ready = [](const saa_turn_ready_ev_t &t) { /* t.audio_pcm16 ... */ };
saa::Client client(cfg);                      // throws std::invalid_argument if refused
client.start_wait(15000);
client.feed_audio_interleaved(buf, frames, 48000, SAA_AUDIO_S16, 2, 0);
```

It needs C++11 and links against the same library. A handler that throws is
caught and logged (through `saa::set_log`), and never reaches the C library.
Without exceptions (`-fno-exceptions`), check `client.valid()` instead.
[`examples/cpp/minimal.cpp`](examples/cpp/minimal.cpp) is the C example in C++.

### 7. A whole voice agent

[`examples/voice_agent`](examples/voice_agent) puts it together on a device.
Each turn goes to OpenAI Realtime, and the reply plays from the device's speaker,
with the device marked as responding around it. An interrupt fades the reply
out, and a turn that arrives while a reply is still being generated cancels it.
It is built with the library, as `build/saa_voice_agent`. With capture:

```bash
export SAA_API_KEY=... OPENAI_API_KEY=...
./build/saa_voice_agent --alsa hw:CARD=Lite --v4l2 /dev/video2 --speaker hw:CARD=Lite
```

Its README covers choosing a speaker, and what to set when the microphone hears
it.

## What to expect

- **Warmup.** For about 12.5 s after `started`, the service fills its window and
  sends placeholder predictions (class 0, confidence 0). Speech in that window
  cannot become a turn. Gate on `on_warmup_complete`, after every
  `on_started`: a reconnect warms up again.
- **Turns include pre-roll.** Send `turn_ready` audio to STT whole; don't gate a
  live mic on the 4 Hz predictions. Turns can be long: a 4.6 s request has come
  back as a 21 s turn, so size your buffers for it.
- **Your own playback.** Call `saa_client_responding_start()` and
  `saa_client_responding_stop()` around the device's TTS. Otherwise the model
  classifies the device's own voice as speech. It still hears whatever the
  microphone picks up of the speaker, so echo cancelling matters.
- **Interrupts.** The service clears responding on its side when it sends
  `interrupt`. A device that keeps talking calls `saa_client_responding_start()`
  again.
- **Mute** stops audio from collecting into the next turn. It does not stop
  audio from being sent, and the turn an interrupt starts still carries the audio
  from before it.
- **Limits.** Session and rate limits are set per evaluation; ask your Attention
  Labs contact.

## API

The API is two headers: `saa/saa_client.h`, and `saa/saa_types.h` for the event
types.

### Lifecycle

| Call | |
|---|---|
| `saa_client_create(&cfg)` | Copies the configuration. Returns NULL if it is invalid: a missing token, a bad URL, or a malformed `server_profile`. |
| `saa_client_start(c)` | Starts the client's thread and returns at once. |
| `saa_client_start_wait(c, ms)` | Starts, then blocks until the service reports `started`, the first connect fails, or `ms` passes (`-1` waits forever). |
| `saa_client_stop(c)` | Closes the connection and joins the thread; safe to call twice. `start` works again afterwards. |
| `saa_client_destroy(c)` | Stops, then frees. |
| `saa_client_is_active(c)` | 1 from `start` until `stop`, or until the session ends by itself on an error it does not retry. With capture there is no feed call to tell you that. |

`start_wait` returns `0`, or one of:
- `SAA_CLIENT_ERR_AUTH`: the key was rejected;
- `SAA_CLIENT_ERR_BUSY`: rate limited, or no capacity;
- `SAA_CLIENT_ERR_TRANSPORT`: DNS, TLS, or socket;
- `SAA_CLIENT_ERR_TIMEOUT`;
- `SAA_CLIENT_ERR_DEVICE`: with capture, the microphone did not open.

The first connect is not retried; call `start_wait` again if you want to.

### Configuration

Zero means the default for every field.

| Field | Meaning |
|---|---|
| `token` | API key; required, never logged |
| `url` | NULL for the hosted service; an `https://` broker or a direct `wss://` URL for testing |
| `video_mode` | `SAA_VIDEO_NONE` (audio only) or `SAA_VIDEO_FEED`; this also picks the server profile |
| `server_profile` | Overrides the profile `video_mode` picks |
| `initial_threshold` | Class-2 confidence threshold, 0.7 by default |
| `auto_reconnect` | 0 reconnects automatically; -1 turns it off |
| `max_reconnect_attempts` | 0 for unlimited |
| `audio_queue_ms` | Audio held while the network is slow, 2000 by default; older audio is dropped |
| `max_message_bytes` | Largest message accepted from the service, 16 MiB by default |
| `utterance_handling` | 1 for the utterance events (preview) |
| `ca_file` | A CA bundle, where the system's does not cover the service |
| `callbacks`, `transport` | The event handlers below; `callbacks.userdata` is passed to all of them |

The capture fields, in builds with capture:

| Field | Meaning |
|---|---|
| `enable_audio` | 1: the library opens the microphone, and `feed_audio` is off |
| `audio_device` | The ALSA device, `default` if NULL |
| `audio_channel` | The channel to keep from a multi-channel device |
| `video_mode` | `SAA_VIDEO_CAPTURE`: the library opens the camera |
| `camera_device` | The V4L2 device, `/dev/video0` if NULL |
| `camera_width`, `camera_height`, `camera_fps` | 640×480, and 4 frames a second sent |

A library built without capture refuses these in `saa_client_create()`, and
logs why.

### Feeding media

- `saa_client_feed_audio()` and `saa_client_feed_audio_interleaved()` accept int16
  or float32, mono or interleaved, at any rate from 8 to 96 kHz. The library
  resamples to 16 kHz and keeps the channel you name. Above 24 kHz it low-passes
  first, so nothing above 8 kHz folds into the speech band: flat to 7 kHz, and
  at least 60 dB down from 8 kHz. At 44.1 kHz the response droops by 0.7 dB at
  7 kHz.
- `saa_client_feed_video()` takes one complete JPEG and needs
  `SAA_VIDEO_FEED`. The newest frame replaces one not yet sent.
- While the client reconnects, both return 0; the data is dropped and counted.
  Nothing is buffered across a reconnect.

### Control

- `saa_client_mute` and `saa_client_unmute`.
- `saa_client_responding_start` and `saa_client_responding_stop`.
- `saa_client_set_threshold`, clamped to [0, 1].
- In preview:
  - `saa_client_add_assistant_turn`, which fails while no connection is open;
  - `saa_client_set_utterance_threshold`;
  - `saa_client_clear_utterance_history`.

After a reconnect, the client re-sends the threshold, mute, responding, and the
utterance threshold on its own.

### Events

`cfg.callbacks` (`saa_callbacks_t`) carries the service's events:

| Callback | When |
|---|---|
| `on_started` | The session is live; `saa_client_session_id()` copies its id |
| `on_warmup_complete` | Predictions mean something from here, once per connection |
| `on_prediction` | Every 250 ms: class 0 not talking, 1 talking to a person, 2 talking to the device; with confidence |
| `on_vad` | Voice activity with each prediction |
| `on_state` | `listening`, `sending`, `cancelled`, `idle` |
| `on_turn_ready` | One utterance meant for the device: 16 kHz PCM, plus any video frames around it |
| `on_config` | The active threshold, echoed after a change |
| `on_interrupt` | The user is taking the turn back mid-response: fade and stop playback over `fade_ms`, and cancel the reply. The next `turn_ready` carries their question |
| `on_interjection` | People talked, then went quiet: the device may volunteer a brief check-in; the event carries the recent audio for context |
| `on_error` | An error; see below |

`cfg.transport` (`saa_transport_callbacks_t`) carries the connection's events:
- `on_connected` and `on_disconnected` (with the close code);
- `on_reconnecting` (the attempt and its delay), then `on_reconnected`;
- `on_stats` every 10 s: RTT, queued bytes, audio and video sent and skipped;
- in preview, `on_utterance_ended` and `on_utterance_config`.

Pointers inside events are only valid during the callback; copy what you keep.

### Errors and reconnects

An `on_error` carries a `kind`, a short `title`, a message, the HTTP status or
close code, and `retriable`.

Once a session has started, a dropped connection reconnects on its own, with
jittered backoff, and `on_reconnecting` reports each attempt. Errors are not
reported while a reconnect is under way, with two exceptions: a stalled
connection, and a key rejected mid-session. The client keeps retrying a
rejected key and reports it each time; call `saa_client_stop()` if the key is
really gone.

The client gives up for good when:
- the service rejects the session outright;
- `max_reconnect_attempts` runs out;
- `auto_reconnect` is off.

It then sends one final `on_error`, titled "Reconnect Failed" when the attempts
ran out, and the feed and control calls return `SAA_CLIENT_ERR_STATE` until you
call `saa_client_stop()`.

### Capture (Linux)

With `SAA_WITH_CAPTURE`, the library opens the microphone and the camera
itself, each on a thread of its own:

```bash
sudo apt install libasound2-dev      # ALSA; V4L2 comes with the kernel headers
cmake -S . -B build -DSAA_WITH_CAPTURE=ON
cmake --build build
```

The capture build's `saaclient.pc` and CMake package also bring in ALSA.

- **Choosing devices.** `arecord -l` lists microphones, and
  `v4l2-ctl --list-devices` cameras. A card's name outlasts its number, which
  can change when the device is plugged in again: prefer `hw:CARD=Lite` to
  `hw:3,0`, and `/dev/v4l/by-id/...` to `/dev/video2`.
- **Permissions.** The user needs to be in the `audio` and `video` groups.
- **Busy devices.** `hw:` and `plughw:` open a card exclusively. While PipeWire,
  or anything else, is using it, the open fails with "Device or resource busy";
  PipeWire holds a card for a few seconds after its last client. `default` goes
  through PipeWire and shares the card.
- **Audio.** The microphone opens at 16 kHz when it has that rate, and otherwise
  at its own, which is resampled. It is read in 20 ms periods; an overrun is
  logged and recovered.
- **Video.** Only cameras that send MJPEG work; the library has no encoder. It
  asks for `camera_width` × `camera_height`, captures at the camera's slowest
  rate that keeps up with `camera_fps`, and sends the newest frame each
  1/`camera_fps` s. Frames are cut at their end marker, and a frame without
  Huffman tables gets the standard ones. A Raspberry Pi ribbon camera offers no
  MJPEG over V4L2: pipe it in with the demo, `rpicam-vid -t 0 --codec mjpeg -o - |
  saa_client_demo --alsa default --mjpeg -`, or send its frames with
  `saa_client_feed_video()`.
- **Starting.** `saa_client_start()` opens the devices before it connects. A
  microphone that will not open fails the start: `on_error` of kind audio, and
  `start_wait` returns `SAA_CLIENT_ERR_DEVICE`. A camera that will not open is
  reported as kind environment, and the session runs audio only. Audio captured
  while the first connection comes up is not sent, and the first `on_stats`
  counts it as skipped, as it counts audio dropped during a reconnect.
- **A device that goes away.** A device that fails mid-session, or delivers
  nothing (1 s for the microphone, 5 s for the camera), gets one retriable
  `on_error` for the outage: "Audio Device Lost" or "Camera Lost". The session
  stays up. The library feeds silence in place of a lost microphone, sends no
  video while the camera is gone, and tries the device again every 2 s.

### Threading

- Callbacks run on the client's own thread. Keep them short.
- `saa_client_feed_audio*()` never blocks or allocates. Call it from one thread
  at a time, such as your audio callback.
- Every other call is safe from any thread.
- `saa_client_stop()` may be called from a callback. It completes after the
  callback returns. `saa_client_destroy()` may not, and is ignored there.
- No callback fires after `saa_client_stop()` returns.

### Logging

`saa_client_set_log_fn(fn, ud)` receives every message, including
libwebsockets' own output, with the API key redacted. By default, warnings and
errors go to stderr. The setting is process-wide, and so is saa-c's control of
libwebsockets' log level: a program that also uses libwebsockets itself shares
it.

### TLS

The system's CA roots are used by default; `ca_file` names another bundle. A
Raspberry Pi has no clock that survives power-off, so TLS fails until it has
synced: check `timedatectl`.

## The demo

`saa_client_demo` streams a WAV, of any rate or channel count, or a live
microphone and camera, and prints each event as a JSON line:
`{"ts_ms", "event", ...fields}`, where `ts_ms` counts from the start. Audio and
JPEG buffers are replaced by their sizes. It needs one audio source, `--wav` or
`--alsa`, and takes at most one video source. SIGINT or SIGTERM stops it
cleanly, with the summary line.

| Option | |
|---|---|
| `--wav FILE` | The audio to stream, paced in real time; `--fast` streams as fast as the client accepts it |
| `--wav -` | A WAV stream on stdin, read as it arrives: `arecord -q -f S16_LE -r 16000 -c 1 -t wav \| saa_client_demo --wav -` |
| `--alsa DEV` | Capture a microphone instead (a capture build) |
| `--channel N` | The channel to keep (0), from `--wav` or `--alsa` |
| `--wait-warmup` | Silence until `warmup_complete`, then the WAV; a stream on stdin starts at once |
| `--tail S` | Seconds of silence after the WAV (3) |
| `--jpeg-dir DIR` | Also send `DIR/*.jpg` at 4 fps |
| `--v4l2 DEV` | Capture an MJPEG camera, with `--alsa` |
| `--size WxH`, `--fps N` | The camera's frame size (640x480), and the frames a second sent from `--v4l2` or `--mjpeg` (4) |
| `--mjpeg -` | JPEG frames on stdin, such as `rpicam-vid -t 0 --codec mjpeg -o -`; the newest is sent each 1/fps s |
| `--audio-only`, `--profile NAME` | Choose the server profile |
| `--threshold F` | The class-2 threshold (0.7) |
| `--utterance` | Utterance handling (preview) |
| `--max-reconnects N` | Give up after N attempts |
| `--events FILE` | JSON lines to a file instead of stdout |
| `--record-turns DIR` | Write each turn to `DIR` as `NNNN.wav` (16 kHz mono), and its frames as `NNNN_k.jpg` |
| `--quiet` | Nothing on stdout and only errors on stderr; use with `--events` |
| `--duration S` | Stop after S seconds |
| `--stats` | Add memory to the stats and turn lines, and the peak to the summary |
| `--url URL`, `--ca FILE` | Another endpoint, another CA bundle |
| `--token KEY` | The key on the command line instead of `SAA_API_KEY`; other users of the machine can see it |
| `--help`, `--version` | |

Exit codes:

| Code | Meaning |
|---|---|
| 0 | The run ended cleanly |
| 2 | The key was rejected |
| 3 | Rate limited, or no capacity |
| 4 | The network or TLS failed |
| 5 | Bad arguments |
| 6 | The microphone did not open |

### JSON lines

The first line's `schema` is the format's version, 1 today. A field changing
its meaning, or going away, bumps it; new fields and events do not.

| Event | Fields |
|---|---|
| `demo_start` | `schema`, `version`, `wav`, `wav_rate`, `wav_channels` (0 without a WAV), `channel`, `alsa`, `v4l2`, `mjpeg` |
| `connected` | |
| `started` | `session_id` |
| `warmup_complete` | |
| `prediction` | `cls`, `raw_cls`, `confidence`, `source` (`model`, `rules`, `ai_responding`), `num_faces`, `responding` |
| `vad` | `probability`, `is_speech` |
| `state` | `state` (`idle`, `listening`, `sending`, `cancelled`) |
| `turn_ready` | `samples`, `duration_sec`, `frames` (each `ts_offset_s`, `bytes`, and `file` when recording), `context`, `server_turn_ready_ts_ms`, `wav` (when recording), `rss_kb`, `rss_anon_kb` (with `--stats`) |
| `config` | `model_class2_threshold` |
| `interrupt` | `fade_ms`, `confidence` |
| `interjection` | `reason`, `samples`, `duration_sec` |
| `utterance_config` | `enabled`, `class1_threshold`, `preview`, `reason` |
| `utterance_ended` | `seq`, `text`, `prediction`, `confidence`, `respond`, `reason`, `start_s`, `end_s`, `truncated`, `assistant_turns`, `preview`, `latency_ms`, `samples` |
| `error` | `kind`, `title`, `message`, `detail`, `code`, `retriable` |
| `disconnected` | `code`, `reason`, `was_clean` |
| `reconnecting` | `attempt`, `delay_ms`, `last_code` |
| `reconnected` | `attempts` |
| `stats` | `rtt_ms`, `queued_bytes`, `sent_audio`, `skipped_audio`, `sent_video`, `skipped_video`, `uptime_ms`, `reconnects`, `rss_kb`, `rss_anon_kb` |
| `wav_end` | |
| `mjpeg_end` | `frames` read, `sent`, `dropped` (malformed or too large), `skipped_bytes` between frames |
| `summary` | `exit_code`, `turns`, `errors`, `peak_rss_kb`, `signal` (0, or the one that stopped the run), `cpu_s`, `wall_s` |

Unknown values are `null`, and memory fields are `-1` without `--stats`.

## Troubleshooting

- **Exit 2:** the key is wrong, or not enabled. The `error` line gives the status.
- **Exit 3:** wait and retry; limits are per evaluation.
- **Exit 4:** check the network, then the clock. A certificate that is "not yet
  valid" means an unsynced clock. Then check any proxy.
- **Exit 6:** the `error` line says why the microphone did not open. "Device or
  resource busy" means another program has the card; see Capture above.
- **No `turn_ready`:**
  - The speech may have started before `warmup_complete`; use `--wait-warmup`.
  - It may not have been addressed to the device.
  - It may be too quiet. `prediction` lines with `"cls":2` show what the model
    heard.

## Build and test

```bash
cmake -S . -B build
cmake --build build
ctest --test-dir build --output-on-failure
```

The tests that stream to the mock server need Python 3 with websockets 13 or
newer. Debian 13 and Raspberry Pi OS trixie ship it (`sudo apt install
python3-websockets`). Elsewhere, including bookworm, whose 10.4 is too old, use a
virtual environment and point CMake at it:

```bash
python3 -m venv ~/saa-venv
~/saa-venv/bin/pip install "websockets>=13"
cmake -S . -B build -DPython3_EXECUTABLE=$HOME/saa-venv/bin/python
```

Without it those tests are skipped with a warning. The build options:
- `-DSAA_REQUIRE_MOCK_TESTS=ON` makes that a configure error.
- `-DSAA_WERROR=ON` treats warnings as errors.
- `-DSAA_BUILD_SHARED=ON` also builds `libsaaclient.so`. Installed to a prefix
  the loader does not search, link with
  `-Wl,-rpath,$(pkg-config --variable=libdir saaclient)`, or set
  `LD_LIBRARY_PATH` (`DYLD_LIBRARY_PATH` on macOS).
- `-DSAA_WITH_CAPTURE=ON` builds the capture module (Linux). Its tests need no
  hardware: fake devices drive the capture paths, and ALSA's `file` plugin
  stands in for a sound card.

The TLS tests need the `openssl` command.

### Cross-compiling

From a Debian or Ubuntu host, the files in `cmake/` build for Raspberry Pi OS
against the target's own libwebsockets, installed from multiarch:

```bash
sudo dpkg --add-architecture armhf && sudo apt update
sudo apt install crossbuild-essential-armhf libwebsockets-dev:armhf qemu-user
cmake -S . -B build-armhf -DCMAKE_TOOLCHAIN_FILE=cmake/arm-linux-gnueabihf.cmake
cmake --build build-armhf
```

Use `aarch64-linux-gnu.cmake` and `arm64` for 64-bit Raspberry Pi OS. Build on
the target's own Debian release, bookworm or trixie: the library is compiled
against that release's libwebsockets, and the two differ.
`tests/cross_check.sh armhf` does all of this, then runs the unit tests and the
harness under qemu. `Dockerfile.cross` does the same in a container.

### Measuring on a device

`tests/device_report.py` builds the library, runs every test, and measures the
library on the machine it runs on:

- size at `-Os`;
- CPU and memory while streaming;
- the latency from a feed call to the frame reaching a local server.

It prints a Markdown report and writes it, with the raw numbers as JSON, to
`build/device-report/`. Run it with a Python that has websockets 13 or newer:

```bash
~/saa-venv/bin/python tests/device_report.py          # add --quick for 15 s runs
SAA_API_KEY=... ~/saa-venv/bin/python tests/device_report.py --live-wav
```

With `--live-wav`, it also streams the sample recording to the service, or the
WAV you name, and checks that a `turn_ready` comes back (`tests/live_smoke.py`).
With `--alsa DEV` and `--v4l2 DEV`, it also builds with capture and measures the
demo capturing from those devices.
On boards with little memory, pass `--jobs 4` to run fewer conformance
scenarios at once.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
