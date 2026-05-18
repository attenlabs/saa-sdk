# syntax=docker/dockerfile:1.6
# Reference Dockerfile for the SAA proactive OpenAI Realtime overlay.
#
# Built from the parent ``examples/`` directory so the image can include
# both ``openai-realtime/`` (the underlying adapter) and
# ``proactive-agent/openai-realtime/`` (this overlay). Build from
# ``examples/proactive-agent/openai-realtime/`` with::
#
#   docker build -t saa-proactive-agent-realtime -f Dockerfile ../..
#   docker run --rm -p 8000:8000 --env-file .env saa-proactive-agent-realtime
#
# The image runs ``proactive_server:app``, which imports the FastAPI
# ``app`` from ``examples/openai-realtime/server.py`` and adds the
# ``POST /proactive-trigger`` + ``GET /proactive-events`` SSE surface.

# Production note: pin `python:3.12-slim` to a specific patch + Debian
# release before deploying (e.g. `python:3.12-slim-bookworm` or a
# digest). `python:3.12-slim` is a moving tag; pin before deploy.
FROM python:3.12-slim AS base

# Match parent openai-realtime/Dockerfile: audioop, opencv, sounddevice
# need a few system libraries even though this relay sets
# enable_audio=False / enable_video=False on the SAA client.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libportaudio2 \
        libgl1 \
        libglib2.0-0 \
        libsndfile1 \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user.
RUN groupadd -r appuser && useradd --no-log-init -r -g appuser appuser

WORKDIR /app

# Layer 1: Python deps (cache-friendly).
COPY proactive-agent/openai-realtime/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Layer 2: parent adapter + overlay. The overlay's proactive_server.py
# adds ../../openai-realtime to sys.path, so /app/openai-realtime/ and
# /app/proactive-agent/openai-realtime/ must coexist.
COPY openai-realtime/ ./openai-realtime/
COPY proactive-agent/openai-realtime/ ./proactive-agent/openai-realtime/

WORKDIR /app/proactive-agent/openai-realtime

EXPOSE 8000

ENV HOST=0.0.0.0 \
    PORT=8000 \
    LOG_LEVEL=info

# The /health route is inherited from the parent examples/openai-realtime/
# server.py app object that proactive_server.py imports and extends.
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://localhost:'+os.environ.get('PORT','8000')+'/health',timeout=2).status==200 else 1)"

USER appuser

CMD ["uvicorn", "proactive_server:app", "--host", "0.0.0.0", "--port", "8000"]
