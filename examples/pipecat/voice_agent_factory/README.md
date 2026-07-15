# SAA voice agent — greenfield factory

A minimal Pipecat + Daily bot that gates speech with **attention labs SAA** via the `build_attention_runner` greenfield factory and a log-only turn handler (no LLM/TTS wired in).

## Environment

Needs its own `.env`:

```
SAA_API_KEY=                 # attention labs API key
DAILY_API_KEY=               # Daily.co REST key from dashboard.daily.co -> Developers
```

## Run

```bash
cp .env.example .env   # fill in SAA_API_KEY + DAILY_API_KEY
python bot.py
```

It creates an ephemeral Daily room and logs a join URL for the human to open.
