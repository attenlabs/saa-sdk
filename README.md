# attenlabs-saa

Python SDK for [Attention Labs](https://attentionlabs.ai) real-time selective auditory attention.

Every voice pipeline has the same problem: the microphone hears everything, but your ASR should only process speech directed at the device. Wake words solve this with a rigid trigger phrase. SAA solves it without one — classifying every audio frame as **silent**, **human-directed**, or **device-directed** and routing only what matters.

`attenlabs-saa` streams mic and webcam data to the SAA inference server over WebSocket and emits typed events: attention predictions, voice activity, conversation state, and ready-to-forward speech audio. LLM routing is left to you.

## Sign up

Get your API key at [attentionlabs.ai](https://attentionlabs.ai).

**You need your API key for this project to work**

## Install

```bash
pip install attenlabs-saa
```

Requires Python 3.10+. `sounddevice` and `opencv-python` are pulled in automatically for mic and camera access.

## Quickstart

```python
import time
from saa import AttentionClient

client = AttentionClient(token="your-token")

@client.on_prediction
def _(event):
    label = {0: "silent", 1: "human", 2: "device"}.get(event.cls, "?")
    print(f"{label}  {event.confidence:.0%}  faces={event.num_faces}  src={event.source}")

@client.on_turn_ready
def _(turn):
    # turn.audio_base64 — base64 PCM16 @ 16 kHz mono, ready for OpenAI Realtime / any LLM
    # turn.audio_pcm16  — same audio as np.int16 array
    print(f"turn ready ({turn.duration_sec:.2f}s)")

@client.on_error
def _(event):
    print(f"ERROR: {event.title}: {event.message}")

client.start()
try:
    while True:
        time.sleep(0.1)
except KeyboardInterrupt:
    client.stop()
```

A full CLI demo wiring SAA + OpenAI Realtime lives at [**saa-py-demo**](https://github.com/attenlabs/saa-py-demo).

---

## API

### `AttentionClient`

```python
from saa import AttentionClient, CameraConfig, MicConfig

client = AttentionClient(
    token="...",                    # Auth token — sent as WS subprotocol
    url=None,                      # Server URL (default: wss://server.attentionlabs.ai/ws)
    video=CameraConfig(),          # Webcam config
    audio=MicConfig(),             # Mic config
    initial_threshold=0.7,         # Device-class confidence threshold (0..1)
    enable_audio=True,             # Set False to skip mic capture
    enable_video=True,             # Set False to skip webcam capture
)
```

### Configuration

#### `MicConfig`

| field      | type                    | default | notes                                      |
| ---------- | ----------------------- | ------- | ------------------------------------------ |
| `device`   | `int \| str \| None`   | `None`  | Device index, name, or `None` for system default |
| `channels` | `int`                   | `1`     | Number of input channels                   |

#### `CameraConfig`

| field          | type  | default | notes                         |
| -------------- | ----- | ------- | ----------------------------- |
| `device_index` | `int` | `0`     | Webcam device index           |
| `width`        | `int` | `1920`  | Capture width                 |
| `height`       | `int` | `1080`  | Capture height                |
| `jpeg_quality` | `int` | `60`    | JPEG compression quality 0–100 |

### Methods

| method                       | description |
| ---------------------------- | ----------- |
| `start()`                    | Opens WebSocket, acquires mic + camera, starts capture threads. Non-blocking. Raises on handshake failure. |
| `stop()`                     | Tears down capture, joins threads, closes WebSocket. |
| `mute()`                     | Pauses upstream audio and signals server to stop VAD. |
| `unmute()`                   | Resumes upstream audio. |
| `mark_responding(bool)`      | Tell the server an LLM response is in flight. Server stops emitting predictions while `True`. |
| `set_threshold(value: float)` | Update device-class confidence threshold (0..1). Server acks via `config` event. |
| `feed_audio(audio, *, sample_rate=16000)` | Stream audio captured by another stack instead of the SDK's own mic. Requires `enable_audio=False`. See [Feeding external audio](#feeding-external-audio). |

### Feeding external audio

When another stack already owns the microphone — an ElevenLabs / OpenAI Realtime `AudioInterface` tap, a Twilio media stream, a game engine — construct the client with `enable_audio=False` and push frames in with `feed_audio()` instead of letting the SDK open its own mic:

```python
client = AttentionClient(token="...", enable_audio=False, enable_video=False)
client.start()                       # opens the WebSocket; captures nothing itself

# in your existing audio callback (any chunk size, mono):
client.feed_audio(pcm_chunk)         # bytes (int16 LE), np.int16, or np.float32 [-1, 1]
```

`feed_audio` accepts arbitrary chunk sizes and re-chunks internally to the wire's 100 ms blocks; pass `sample_rate=` if your audio isn't already 16 kHz and it'll resample. Calling it while `enable_audio=True` raises (that would double the audio source). A runnable ElevenLabs Conversational AI example lives in [`saa-sdk/examples/elevenlabs`](https://github.com/attenlabs/saa-sdk/tree/main/examples/elevenlabs).

### Events

Register handlers with decorators. All callbacks fire on internal threads — keep them fast or hand work off to your own thread.

```python
@client.on_prediction
def handle(event):
    ...
```

| decorator             | payload                                                                  | fires when                              |
| --------------------- | ------------------------------------------------------------------------ | --------------------------------------- |
| `@on_connected`       | —                                                                        | WebSocket opens                         |
| `@on_started`         | —                                                                        | Server-side warmup complete             |
| `@on_warmup_complete` | —                                                                        | First non-zero-confidence prediction    |
| `@on_prediction`      | `PredictionEvent`                                                        | Each attention prediction               |
| `@on_vad`             | `VadEvent`                                                               | Voice activity update                   |
| `@on_state`           | `StateEvent`                                                             | Conversation state transition           |
| `@on_turn_ready`      | `TurnReadyEvent`                                                         | Complete user turn ready to forward     |
| `@on_config`          | `ConfigEvent`                                                            | Server acks a threshold change          |
| `@on_stats`           | `StatsEvent`                                                             | Every ~10s with connection health       |
| `@on_interrupt`       | `InterruptEvent`                                                         | User is barging in mid-LLM-response     |
| `@on_error`           | `AttentionErrorEvent`                                                    | Connection, auth, or server error       |
| `@on_disconnected`    | `DisconnectedEvent`                                                      | WebSocket closes                        |

### Event types

#### `PredictionEvent`

```python
cls: int            # 0 = silent, 1 = human-directed, 2 = device-directed
confidence: float   # 0..1
source: str         # "video" or "audio"
num_faces: int      # faces detected in frame
```

#### `VadEvent`

```python
probability: float  # VAD probability 0..1
is_speech: bool     # whether speech was detected
```

#### `StateEvent`

```python
state: ConversationState  # "listening" | "sending" | "cancelled" | "idle"
```

#### `TurnReadyEvent`

```python
audio_pcm16: np.ndarray   # int16 array @ 16 kHz mono
audio_base64: str          # same audio as base64 — ready for OpenAI Realtime, etc.
duration_sec: float        # duration in seconds
frames: list[TurnFrame]    # JPEG stills, empty unless the server has frames_per_turn > 0
```

#### `ConfigEvent`

```python
model_class2_threshold: float  # server-confirmed threshold
```

#### `StatsEvent`

```python
rtt_ms: float | None  # round-trip latency in ms
sent_video: int        # total video frames sent
skipped_video: int     # total video frames skipped
sent_audio: int        # total audio chunks sent
uptime_s: float        # connection uptime in seconds
```

#### `InterruptEvent`

```python
fade_ms: int        # suggested fade duration (ms) before stopping playback
confidence: float   # raw model confidence of the class-2 prediction that fired
```

Fires when the server detects the user trying to take the turn back while
the LLM is mid-response. The server has already moved its state machine to
`listening` and pre-rolled the user's recent audio into the next turn — the
following `turn_ready` event will carry the actual barge-in question. The
consumer's job is to (a) fade and stop its local LLM playback over
`fade_ms`, (b) cancel any in-flight LLM response, and (c) re-open the mic
immediately (do not wait for the fade to finish, or the user's continued
speech is dropped for the duration of the fade).

#### `AttentionErrorEvent`

```python
title: str                  # error category ("Auth Failed", "Connection Stalled", etc.)
message: str                # human-readable message
detail: str | None = None   # technical detail
code: int | None = None     # WebSocket close code, if applicable
```

#### `DisconnectedEvent`

```python
code: int        # WebSocket close code
reason: str      # close reason
was_clean: bool  # True if code == 1000
```

---

## LLM integration

LLM routing is intentionally **not** part of the SDK. The `turn_ready` event hands you PCM16 audio — both as a NumPy array and as base64 — forward it wherever you like.

When your LLM starts generating, call `mute()` + `mark_responding(True)` to suppress predictions during playback. When it finishes, `unmute()` + `mark_responding(False)`.

```python
from saa import AttentionClient

client = AttentionClient(token="...")

@client.on_turn_ready
def _(turn):
    # Forward to your LLM of choice
    your_llm.send(turn.audio_base64)

def on_llm_speaking():
    client.mute()
    client.mark_responding(True)

def on_llm_done():
    client.unmute()
    client.mark_responding(False)
```

### Barge-in (interrupt) handling

When the server detects the user trying to take the turn back while the
LLM is speaking, it fires `interrupt`. Wire it to a fade-and-cancel on
your LLM playback layer, then re-open the mic immediately:

```python
@client.on_interrupt
def _(event):
    # Fade your local LLM audio and cancel its in-flight response.
    your_llm.interrupt(event.fade_ms)
    # Re-open the mic immediately — do NOT wait for the fade to finish,
    # or the user's continued speech is dropped for the fade duration.
    client.unmute()
    client.mark_responding(False)
```

The server has already moved its state machine to `listening` and
pre-rolled the user's recent audio into the chunk accumulator by the time
this event arrives. The next `turn_ready` event will carry the user's
actual barge-in question.

See [**saa-py-demo**](https://github.com/attenlabs/saa-py-demo) for a full working example with OpenAI Realtime.

## Threading model

The SDK manages four threads internally:

| thread           | purpose                            |
| ---------------- | ---------------------------------- |
| `saa-ws`         | WebSocket send/receive             |
| `saa-heartbeat`  | JSON pings every 5s, stats every 10s |
| `saa-camera`     | JPEG capture at 4 fps (250 ms)     |
| *(sounddevice)*  | Audio callback at native sample rate, resampled to 16 kHz |

All event callbacks fire on `saa-ws` or `saa-heartbeat`. Don't block them — offload heavy work to your own thread.

## License

Apache-2.0
