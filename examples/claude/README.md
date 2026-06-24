# SAA + Claude voice agent

SAA gates the microphone so only speech directed at the device reaches Claude.
Side talk, background voices, and the agent's own TTS playback are filtered out
before any audio touches the Anthropic API.

```
                mic + webcam
                     │
                 ┌───▼───┐
                 │  SAA  │  attenlabs-saa (streaming SDK)
                 └───┬───┘
          (device-directed turns only)
                     │
           ┌─────────▼─────────┐
           │  Claude Messages  │  anthropic-sdk
           │  claude-sonnet-4-6│
           └─────────┬─────────┘
                     │
                  pyttsx3 TTS
```

## Prerequisites

- Python 3.10+
- An attention labs API key: [attentionlabs.ai/dashboard](https://attentionlabs.ai/dashboard)
- An Anthropic API key: [console.anthropic.com](https://console.anthropic.com)

## Install

```bash
pip install -r requirements.txt
```

## Configure

```bash
cp .env.example .env
# edit .env and fill in SAA_API_KEY and ANTHROPIC_API_KEY
```

Or export directly:

```bash
export SAA_API_KEY=...
export ANTHROPIC_API_KEY=...
```

## Run

```bash
python agent.py                    # mic + webcam, TTS on
python agent.py --audio-only       # no webcam (faster for laptops)
python agent.py --no-tts           # print Claude's replies instead of speaking
python agent.py --threshold 0.75   # adjust SAA confidence threshold
```

## How it works

1. `AttentionClient` opens a WebSocket to SAA and starts capturing mic + webcam.
2. SAA classifies every audio frame as `silent` / `human-directed` / `device-directed`.
3. On `turn_ready` SAA fires with a complete utterance (PCM16 @ 16 kHz mono).
4. The agent wraps the raw audio in a minimal WAV header and sends it to Claude's Messages API as an audio input block, alongside the conversation history.
5. Claude's text response is printed and optionally spoken aloud via pyttsx3.
6. During playback `mute()` and `mark_responding(True)` suppress SAA predictions, so the agent's own voice does not trigger another turn.
7. If the user talks over the agent, SAA fires `interrupt` and the agent stops speaking immediately.

## Options

| Flag | Default | Notes |
|---|---|---|
| `--audio-only` | off | Disables webcam; SAA runs in audio-only mode |
| `--no-tts` | off | Prints replies; no audio playback |
| `--threshold` | `0.7` | SAA confidence to treat speech as device-directed (0–1) |

Recommended thresholds (from the SAA examples README):

- With webcam: `0.6`, `0.77`, `0.88`
- Audio-only: `0.5`, `0.7`, `0.8`

## Conversation history

The agent maintains full multi-turn history (`self._history`) across turns, so Claude has context from earlier in the session. History is held in memory only and resets on restart.

## Dependencies

| Package | Purpose |
|---|---|
| `attenlabs-saa` | SAA streaming SDK — addressee classification, audio capture |
| `anthropic` | Claude Messages API client |
| `pyttsx3` | Local text-to-speech (optional; text-only mode works without it) |

## See also

- [`packages/saa-py/README.md`](../../packages/saa-py/README.md) — full streaming SDK reference
- [`examples/python/`](../python/) — streaming SDK demo with OpenAI Realtime
- [`examples/elevenlabs/`](../elevenlabs/) — ElevenLabs `feed_audio` pattern (same shape as this example)
