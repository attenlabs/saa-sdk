# syntax=docker/dockerfile:1.6
# Reference Dockerfile for the SAA proactive ElevenLabs CAI overlay.
#
# Built from the parent ``examples/`` directory so the image can include
# both ``elevenlabs-cai/`` (the underlying adapter) and
# ``proactive-agent/elevenlabs-cai/`` (this overlay). Build from
# ``examples/proactive-agent/elevenlabs-cai/`` with::
#
#   docker build -t saa-proactive-agent-elevenlabs -f Dockerfile ../..
#   docker run --rm -p 8000:8000 --env-file .env saa-proactive-agent-elevenlabs
#
# The image runs ``proactive_server:app``, which imports the FastAPI
# ``app`` from ``examples/elevenlabs-cai/server.py`` (the token-minting
# server) and adds the proactive trigger surface.

# Production note: pin `python:3.12-slim` to a specific patch + Debian
# release before deploying (e.g. `python:3.12-slim-bookworm` or a
# digest). `python:3.12-slim` is a moving tag; pin before deploy.
FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user.
RUN groupadd -r appuser && useradd --no-log-init -r -g appuser appuser

WORKDIR /app

# Layer 1: Python deps (cache-friendly).
COPY proactive-agent/elevenlabs-cai/requirements.txt ./overlay-requirements.txt
COPY elevenlabs-cai/requirements.txt ./parent-requirements.txt
RUN pip install --no-cache-dir \
        -r ./parent-requirements.txt \
        -r ./overlay-requirements.txt

# Layer 2: parent adapter + overlay. The overlay's proactive_server.py
# adds ../../elevenlabs-cai to sys.path; /app/elevenlabs-cai/ and
# /app/proactive-agent/elevenlabs-cai/ must coexist.
COPY elevenlabs-cai/ ./elevenlabs-cai/
COPY proactive-agent/elevenlabs-cai/ ./proactive-agent/elevenlabs-cai/

WORKDIR /app/proactive-agent/elevenlabs-cai

EXPOSE 8000

ENV HOST=0.0.0.0 \
    PORT=8000 \
    LOG_LEVEL=info

# The /health route is inherited from the parent examples/elevenlabs-cai/
# server.py app object that proactive_server.py imports and extends.
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://localhost:'+os.environ.get('PORT','8000')+'/health',timeout=2).status==200 else 1)"

USER appuser

CMD ["uvicorn", "proactive_server:app", "--host", "0.0.0.0", "--port", "8000"]
