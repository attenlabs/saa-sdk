# SAA + ElevenLabs Conversational AI

A sample that enables your [ElevenLabs Conversational AI](https://elevenlabs.io/docs/eleven-agents/overview) agent to only respond when people are talking to it, and stay silent to side conversations or background voices.

## How SAA integrates

ElevenLabs runs its agent inside its own sealed WebRTC room, so this sample uses the streaming SDK:

1. ElevenLabs' Python SDK exposes `AudioInterface`, a clean 16-bit-PCM seam for the user mic and the agent's TTS.
2. The sample wraps it and feeds the user mic to SAA via [`attenlabs-saa`](../../packages/saa-py)'s `feed_audio()` (the SDK is in feed mode: `enable_audio=False`, it captures nothing itself).
3. SAA classifies each frame and emits `prediction` / `vad` / `interrupt` events.
4. The sample gates the agent on those events and forwards only device-directed audio onward.

## Samples

| Sample | Stack | Run |
|---|---|---|
| [`voice_agent/`](./voice_agent) | ElevenLabs Conversational AI (managed speech-to-speech), SAA-gated via `feed_audio` | `python agent.py` |

## Quick start

Needs **Python 3.10 to 3.12** and an ElevenLabs agent ID.

```bash
cd examples/elevenlabs
cp .env.example .env     # fill SAA_API_KEY, ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID

cd voice_agent
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ../../../packages/saa-py              # local dev against this repo
pip install -r requirements.txt
python agent.py
```

The sample **auto-loads** `examples/elevenlabs/.env`, so the command is identical on Windows, macOS, and Linux.

### Windows notes

`attenlabs-saa` is pure Python and installs cleanly. The only Windows friction is `pyaudio`
(pulled in by `elevenlabs[pyaudio]` for microphone capture), which has no wheels for the
newest Python and otherwise compiles from source against PortAudio.

1. **Use Python 3.11 or 3.12.**
2. **Install `pyaudio` from a wheel, not source**, before `pip install -r requirements.txt`:
   - `pip install pipwin && pipwin install pyaudio`, or
   - Conda: `conda install -c anaconda pyaudio`, or
   - download a matching `.whl` and `pip install path\to\PyAudio...whl`.
3. **If a long-path error appears** when cloning into a deep folder, enable long paths once in an Administrator PowerShell, then retry:
   ```powershell
   Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name "LongPathsEnabled" -Value 1
   ```
   or clone closer to the drive root.

## The lines that integrate SAA

```python
saa = AttentionClient(token=SAA_API_KEY, enable_audio=False, enable_video=False)
attn = SAAFeedAudioInterface(DefaultAudioInterface(), saa)   # tee + feed_audio + gate

@saa.on_prediction
def _(ev): attn.update_gate(ev.cls)            # only device-directed audio reaches the agent

conversation = Conversation(..., audio_interface=attn)
saa.start(); conversation.start_session()
```

`SAAFeedAudioInterface` also drives `saa.mark_responding()` from the agent's TTS, so SAA knows when the agent is the one speaking. See [`voice_agent/README.md`](./voice_agent/README.md) for the full walk-through and tradeoffs.

## Shared environment

One env file, [`.env`](./.env.example) in this directory:

| Key | Purpose |
|---|---|
| `SAA_API_KEY` | Your Attention Labs API key. Get one at [attentionlabs.ai/dashboard](https://attentionlabs.ai/dashboard). |
| `ELEVENLABS_API_KEY` | ElevenLabs API key |
| `ELEVENLABS_AGENT_ID` | The agent to talk to |

## Requirements & limitations

- **Audio-only**, ElevenLabs gives SAA no video, so class-1 ("talking to a human") is weaker than on the multimodal LiveKit / Pipecat paths.
- The gate opens ~250 ms after device-directed speech starts (classifier latency); a hard gate can clip an utterance's first syllable. See the sample README for the softer `register_user_activity` alternative.
- Interjection is JS-only today, so it isn't wired in this Python sample.
- Barge-in is handled by ElevenLabs' own VAD.
