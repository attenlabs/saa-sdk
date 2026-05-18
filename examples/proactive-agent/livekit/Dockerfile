# syntax=docker/dockerfile:1.6
# Reference Dockerfile for the SAA proactive LiveKit Agents overlay.
#
# Built from the parent ``examples/`` directory so the image can include
# both ``livekit/`` (the underlying adapter) and
# ``proactive-agent/livekit/`` (this overlay). Build from
# ``examples/proactive-agent/livekit/`` with::
#
#   docker build -t saa-proactive-agent-livekit -f Dockerfile ../..
#   docker run --rm --env-file .env saa-proactive-agent-livekit dev
#
# The image runs ``proactive_agent:main`` which wraps the parent's
# entrypoint with an HTTP /trigger sidecar on PROACTIVE_HTTP_PORT.

# Production note: pin `python:3.12-slim` to a specific patch + Debian
# release before deploying (e.g. `python:3.12-slim-bookworm` or a
# digest). `python:3.12-slim` is a moving tag; pin before deploy.
FROM python:3.12-slim AS base

# Match parent livekit/Dockerfile system deps (sounddevice, opencv,
# silero VAD wheel, ffmpeg for the livekit-agents plugins).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libportaudio2 \
        portaudio19-dev \
        libgl1 \
        libglib2.0-0 \
        libsndfile1 \
        ffmpeg \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. (The parent livekit/Dockerfile keeps root
# because of the build-time model pre-warm; this overlay has no such
# step, so it switches to non-root for the final CMD.)
RUN groupadd -r appuser && useradd --no-log-init -r -g appuser appuser

WORKDIR /app

# Layer 1: parent's pinned deps + overlay deps.
COPY livekit/requirements.txt /tmp/parent-requirements.txt
COPY proactive-agent/livekit/requirements.txt /tmp/overlay-requirements.txt
RUN pip install --no-cache-dir \
        -r /tmp/parent-requirements.txt \
        -r /tmp/overlay-requirements.txt

# Layer 2: parent adapter + overlay sources.
COPY livekit/ ./livekit/
COPY proactive-agent/livekit/ ./proactive-agent/livekit/

WORKDIR /app/proactive-agent/livekit

ENV PROACTIVE_HTTP_PORT=8765

EXPOSE 8765

USER appuser

# CMD takes one of: dev, console, start. Default is dev for fast iteration.
ENTRYPOINT ["python", "proactive_agent.py"]
CMD ["dev"]
