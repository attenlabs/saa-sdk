# attenlabs-saa-demo

Build agents that know when the user is actually talking to them. Save on tokens.

Python sample app for [attention labs](https://attentionlabs.ai) real-time selective auditory attention (SAA).

Every voice pipeline has the same problem: the microphone hears everything, but your ASR should only process speech directed at the device. Wake words solve this with a rigid trigger phrase. SAA solves it without one.

That makes SAA useful for robots, smart displays, TVs, desktop agents, AR/VR interfaces, and other voice AI systems that need to ignore background conversation while still feeling natural.

[demo.webm](https://github.com/user-attachments/assets/14c5a350-9059-4ac7-bba9-92dca01feb69)

---

## How it works

`attenlabs-saa` streams mic and webcam data to the SAA inference server over WebSocket and emits typed events: attention predictions, voice activity, conversation state, and speech audio.

```text
Mic + Webcam
     |
     v
attenlabs-saa SDK
     |
     | WebSocket
     v
SAA inference server
     |
     +-- prediction events  (ConvoStatus 0 / 1 / 2)
     +-- conversation state events
     +-- turn_ready audio + optional frames
              |
              v
     Optional ASR + LLM / Agent
              |
              v
        Speaker playback
```

This sample app uses OpenAI Realtime Voice as the LLM stage, but the bridge is part of the demo, not the SDK. Swap in whichever provider you like.

---

## Quickstart

Follow the steps in order. Should take about 5 minutes.

### 1. Get a SAA auth token

Sign up at the [attention labs dashboard](https://attentionlabs.ai/dashboard/) and copy your token.

### 2. (Optional) Get an OpenAI API key

You only need this if you want the LLM to talk back. Use a key with Realtime API access. Skip this step to just see live SAA predictions in the terminal.

### 3. Check your environment

- Python 3.10+
- A microphone and webcam
- **Grant your terminal microphone + camera permissions.** On macOS: System Settings → Privacy & Security → Microphone / Camera → enable for Terminal (or iTerm, VS Code, etc). The app will fail silently without this.

### 4. Install system audio dependencies

macOS:
```bash
brew install portaudio
```

Linux (Debian/Ubuntu):
```bash
sudo apt-get install -y libportaudio2 libasound2-dev
```

### 5. Install the demo

From a checkout of the [saa-sdk](https://github.com/attenlabs/saa-sdk) monorepo:

```bash
cd examples/python
pip install attenlabs-saa cv2-enumerate-cameras simpleaudio
```

### 6. Run it

With the LLM stage enabled:
```bash
python main.py --token YOUR_SAA_API_KEY --openai-key sk-...
```

Without an LLM (just see live predictions):
```bash
python main.py --token YOUR_SAA_API_KEY --no-llm
```

Or use env vars:
```bash
export SAA_API_KEY=...
export OPENAI_API_KEY=sk-...
python main.py
```

---

## What to expect

After a short warmup, the app prints a live status panel:

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  ATTENTION LABS :: CONVERSATION INTELLIGENCE v1.0                            ║
║  Press Ctrl+C to stop                                                        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  CURRENT MODE : TALKING TO COMPUTER (94%)                                    ║
║  BUFFER       : [0, 0, 0, 2, 2, 2, 2, 2, 2, 2]                               ║
║  LLM STATE    : listening                                                    ║
║  PROCESSING   : 16.2s                                                        ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

- **CURRENT MODE**, the latest ConvoStatus prediction and confidence
- **BUFFER**, rolling window of the last 10 predictions
- **LLM STATE**, `idle` → `listening` → `processing` → `speaking`

Once enough consecutive `2`s land in the buffer, the LLM state flips to `listening`. When you stop talking (or turn to talk to someone else) the captured audio is sent to OpenAI Realtime, and the reply plays through your speakers.

---

## ConvoStatus states

Every `prediction` event carries a ConvoStatus value:

| State | Meaning                                            |
|-------|----------------------------------------------------|
| `0` | Silence, no speech detected                         |
| `1` | Human-to-human, people are talking to each other    |
| `2` | Human-to-device, someone is talking to the computer |

Your pipeline only needs to act on state `2`. States `0` and `1` let you skip ASR entirely and avoid sending irrelevant audio to your LLM. 

---

## Tuning

- `--threshold` (default `0.75`): minimum confidence for a state-`2` prediction to count as device-directed.
- Turn detection is handled by the SAA server/SDK (`on_turn_ready`); the demo just forwards the captured turn. `_BUFFER_LEN` in [main.py](main.py) only sizes the on-screen prediction history, not the trigger.

Lower the threshold for more sensitive triggering; raise it for fewer false starts.

---

## CLI options

```
--token             SAA auth token (required; or SAA_API_KEY env var)
--url               Override the default SAA server URL (for self-hosted SAA)
--openai-key        OpenAI API key; falls back to OPENAI_API_KEY env var
--camera-index      Webcam device index (skip the picker)
--mic-device        Mic device name or numeric index (skip the picker)
--threshold         Device-class trigger threshold 0..1 (default 0.75)
--no-video          Disable webcam capture
--no-audio          Disable mic capture
--no-llm            Disable LLM stage even if a key is set
--log-level         DEBUG, INFO, WARNING, ERROR (default WARNING)
```

---

## SDK docs

Full API reference, constructor, methods, events, types, threading model, lives in the [Python SDK reference](https://attentionlabs.ai/docs/python/reference/).

---

## Security note

This demo reads the OpenAI API key from a CLI arg or env var and uses it directly from the local process. Fine for personal use. For multi-user deployments, proxy the Realtime connection through a server you control so keys never leave your backend.

---

## License

Apache-2.0
