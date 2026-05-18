<p align="center">
  <a href="../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../assets/saa-hero-light.svg" width="326">
  </a>
</p>

# twilio

SAA is a pre-STT gate for an inbound or outbound phone call carried over Twilio Media Streams. μ-law 8 kHz → PCM16 16 kHz transcode happens inside the relay; only device-directed speech reaches your downstream LLM bridge.

| | |
|---|---|
| Run             | `make dev` (Twilio webhook → `http://localhost:8000/voice`) |
| Demonstrates    | A Twilio Media Streams relay gated by SAA, with three swappable downstream bridges: OpenAI Realtime, Deepgram + OpenAI + ElevenLabs, or your own. Inbound + outbound supported. |
| Expect          | The caller's voice gates; background office audio, in-cabin radio, and the agent's own playback stay outside the gate. The downstream LLM only sees device-directed turns. |
| Known gotcha    | Phone calls have no video, so SAA operates audio-only. Tune the threshold per noise floor; the default `0.70` is a safe starting point. |

## Run

```bash
cp .env.example .env       # ATTENLABS_TOKEN + Twilio + downstream-LLM keys
make install
make dev                   # FastAPI relay on :8000
make tunnel                # optional: ngrok so Twilio can reach /voice
```

Point a Twilio number's Voice webhook at `https://<your-host>/voice`.

## Bridges (pick one)

| Bridge | Module | When to pick it |
|---|---|---|
| **OpenAI Realtime** | [`bridge_openai_realtime.py`](./bridge_openai_realtime.py) | Lowest latency. Single vendor for STT + LLM + TTS. |
| **Deepgram + OpenAI + ElevenLabs** | [`bridge_deepgram_openai_elevenlabs.py`](./bridge_deepgram_openai_elevenlabs.py) | You want to pick best-in-class per stage, or you want detailed transcripts. |
| **Your own** | implement [`bridge.Bridge`](./bridge.py) | Anything else. The abstract base is ~80 lines. |

The relay (`server.py`) is bridge-agnostic; switch via the `BRIDGE` env var.

## Env vars

| Var | Required | Default |
|---|---|---|
| `ATTENLABS_TOKEN` | yes | |
| `SAA_THRESHOLD` | no | `0.7` |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | yes for outbound | |
| `PUBLIC_HOSTNAME` | yes for outbound (Twilio dials back) | |
| `OPENAI_API_KEY` | for bridges A & B | |
| `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` | for bridge B | |
| `HOST`, `PORT` | no | `0.0.0.0`, `8000` |

## Files

- [`server.py`](./server.py): FastAPI app providing `/voice` (TwiML), `/stream` (Media Streams WS), and `/dial` (outbound).
- [`audio.py`](./audio.py): pure-NumPy μ-law ↔ PCM16 codec at 8 / 16 kHz.
- [`bridge.py`](./bridge.py): abstract bridge interface (one `Bridge.handle_turn` method).
- [`bridge_openai_realtime.py`](./bridge_openai_realtime.py), [`bridge_deepgram_openai_elevenlabs.py`](./bridge_deepgram_openai_elevenlabs.py): reference bridges.
- [`twiml.py`](./twiml.py): TwiML response builder.
- [`outbound.py`](./outbound.py): paced outbound bytes (avoids overrunning Twilio's buffer).

## Recording disclosure

The relay does not record by default. If your jurisdiction requires two-party consent, add a recorded prompt before opening the Media Stream. Consult counsel; the relay is bridge-agnostic on this.

## Tests

```bash
make test-shape          # no-network shape check (CI uses this)
make test                # full pytest suite
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Twilio dials but the call drops in 5 s | Twilio needs a public HTTPS endpoint. Use `make tunnel` for dev. |
| Audio is garbled | The relay assumes μ-law 8 kHz inbound; verify Twilio's stream `<Track>` is `inbound_track` only. |
| Threshold mistuned | Phone calls have a higher false-trigger rate than laptop multimodal. Sweep `SAA_THRESHOLD` between `0.65` and `0.82` against your own recordings. |
| Outbound calls don't dial | Set `TWILIO_FROM_NUMBER` and `PUBLIC_HOSTNAME`; the latter must be reachable from Twilio's POP. |

## See also

- [`packages/saa-py/README.md`](../../packages/saa-py/README.md): SDK reference.
- [`packages/saa-gate/README.md`](../../packages/saa-gate/README.md): the cross-framework routing policy.
- [`examples/proactive-agent/twilio/`](../proactive-agent/twilio/): the AI-SDR proactive overlay built on this adapter.
- [`examples/README.md`](../README.md): adapter index.


---

<p align="center">
  <sub>An Attention Labs project. © 2026.</sub>
</p>
