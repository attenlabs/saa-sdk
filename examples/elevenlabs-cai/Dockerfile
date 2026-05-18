# syntax=docker/dockerfile:1.6
# Reference Dockerfile for the SAA × ElevenLabs Conversational AI server.
#
#   docker build -t saa-elevenlabs-cai .
#   docker run --rm -p 8080:8080 --env-file .env saa-elevenlabs-cai
#
# The image bundles the FastAPI server: it serves the browser bundle on /,
# mints ElevenLabs auth blobs at /api/conversation-token + /api/signed-url
# (xi-api-key never leaves the server), and runs the Twilio ↔ SAA ↔
# ElevenLabs CAI WebSocket relay at /twilio.

# Production note: pin `python:3.12-slim` to a specific patch + Debian
# release before deploying (e.g. `python:3.12-slim-bookworm` or a
# digest). `python:3.12-slim` is a moving tag that tracks both Python
# patch releases and Debian base updates; pinning is the reproducibility
# guarantee. Same advice in the other reference Dockerfiles.
FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. Created here so all subsequent COPY/RUN steps
# happen as root (for /usr/local/lib pip installs) but the final CMD
# runs unprivileged.
RUN groupadd -r appuser && useradd --no-log-init -r -g appuser appuser

WORKDIR /app

# Layer 1: Python deps (cache-friendly).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Layer 2: server + browser bundle + TwiML reference.
COPY server.py index.html main.js twiml.xml ./

ENV HOST=0.0.0.0 \
    PORT=8080 \
    LOG_LEVEL=info

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://localhost:'+os.environ.get('PORT','8080')+'/health',timeout=2).status==200 else 1)"

USER appuser

CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
