# attenlabs-saa SDK Reference

Python SDK for [Attention Labs](https://attentionlabs.ai) real-time selective auditory attention.

Every voice pipeline has the same problem: the microphone hears everything, but your ASR should only process speech directed at the device. Wake words solve this with a rigid trigger phrase. SAA solves it without one — classifying every audio frame as **silent**, **human-directed**, or **device-directed** and routing only what matters.

`attenlabs-saa`  mic and webcam data is sent to the SAA inference server over WebSocket and emits typed events: attention predictions, voice activity, conversation state, and speech audio. You can adjust when to send to the LLM.

## Sign up

Get your API token at [attentionlabs.ai/dashboard](https://attentionlabs.ai/dashboard).

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
def _(event):
    # event.audio_base64 — base64 PCM16 @ 16 kHz mono, ready for OpenAI Realtime / any LLM
    # event.audio_pcm16  — same audio as np.int16 array
    # event.frames       — list[TurnFrame], empty unless enabled server-side
    print(f"turn ready ({event.duration_sec:.2f}s, {len(event.frames)} frames)")

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

| field      | type                    | default | notes                                            |
| ---------- | ----------------------- | ------- | ------------------------------------------       |
| `device`   | `int \| str \| None`    | `None`  | Device index, name, or `None` for system default |
| `channels` | `int`                   | `1`     | Number of input channels                         |

#### `CameraConfig`

| field          | type  | default | notes                          |
| -------------- | ----- | ------- | ------------------------------ |
| `device_index` | `int` | `0`     | Webcam device index            |
| `width`        | `int` | `1920`  | Capture width                  |
| `height`       | `int` | `1080`  | Capture height                 |
| `jpeg_quality` | `int` | `60`    | JPEG compression quality 0–100 |

### Methods

| method                       | description                                                                                                |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `start()`                    | Opens WebSocket, acquires mic + camera, starts capture threads. Non-blocking. Raises on handshake failure. |
| `stop()`                     | Tears down capture, joins threads, closes WebSocket.                                                       | 
| `mute()`                     | Pauses upstream audio and signals server to stop VAD.                                                      |
| `unmute()`                   | Resumes upstream audio.                                                                                    |
| `mark_responding(bool)`      | Tell the server an LLM response is in flight. Server stops emitting predictions while `True`.              |
| `set_threshold(value: float)`| Update device-class confidence threshold (0..1). Server acks via `config` event.                           |

### Events

Register handlers with decorators. All callbacks fire on internal threads.

```python
@client.on_prediction
def handle(event):
    ...
```

| decorator             | payload                | fires when                               |
| --------------------- | ---------------------- | ---------------------------------------- |
| `@on_connected`       | —                      | WebSocket opens                          |
| `@on_started`         | —                      | Server-side warmup complete              |
| `@on_warmup_complete` | —                      | First non-zero-confidence prediction     |
| `@on_prediction`      | `PredictionEvent`      | Each attention prediction                |
| `@on_vad`             | `VadEvent`             | Voice activity update                    |
| `@on_state`           | `StateEvent`           | Conversation state transition            |
| `@on_turn_ready`      | `TurnReadyEvent`       | Complete user turn ready to forward (audio + optional frames) |
| `@on_config`          | `ConfigEvent`          | Server acks a threshold change           |
| `@on_stats`           | `StatsEvent`           | Every ~10s with connection health        |
| `@on_error`           | `AttentionErrorEvent`  | Connection, auth, or server error        |
| `@on_disconnected`    | `DisconnectedEvent`    | WebSocket closes                         |

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
audio_pcm16: np.ndarray    # int16 array @ 16 kHz mono
audio_base64: str           # same audio as base64 — ready for OpenAI Realtime, etc.
duration_sec: float         # duration in seconds
frames: list[TurnFrame]     # sampled JPEG stills; empty unless enabled server-side
```

#### `TurnFrame`

```python
ts_offset_s: float          # seconds from listening-start; negative = pre-context frame
image_base64: str           # base64 JPEG (no data: prefix — add one yourself if wrapping)
```

Frames are sampled evenly across the listening window, plus one optional pre-context
frame from ~1 s before the user started speaking. Off by default in the SDK; the
hosted SAA turns them on at 3 frames per turn.

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

LLM routing is intentionally **not** part of the SDK. The `turn_ready` event hands you PCM16 audio — both as a NumPy array and as base64 — plus any sampled JPEG frames the server attached. Wrap them into your LLM's content format yourself; the SDK stays vendor-neutral.

When your LLM starts generating, call `mute()` + `mark_responding(True)` to suppress predictions during playback. When it finishes, `unmute()` + `mark_responding(False)`.

```python
from saa import AttentionClient

client = AttentionClient(token="...")

@client.on_turn_ready
def _(event):
    # Forward audio + optional frames to your LLM of choice
    your_llm.send(event.audio_base64, frames=event.frames)

def on_llm_speaking():
    client.mute()
    client.mark_responding(True)

def on_llm_done():
    client.unmute()
    client.mark_responding(False)
```

See [`main.py`](main.py) in this repo for a full working example with OpenAI Realtime.

## Threading model

The SDK manages four threads internally:

| thread           | purpose                                                   |
| ---------------- | --------------------------------------------------------- |
| `saa-ws`         | WebSocket send/receive                                    |
| `saa-heartbeat`  | JSON pings every 5s, stats every 10s                      |
| `saa-camera`     | JPEG capture at 4 fps (250 ms)                            |
| *(sounddevice)*  | Audio callback at native sample rate, resampled to 16 kHz |

All event callbacks fire on `saa-ws` or `saa-heartbeat`.

## License

MIT
