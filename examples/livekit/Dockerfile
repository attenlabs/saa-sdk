# syntax=docker/dockerfile:1.6
# Reference Dockerfile for the SAA × LiveKit Agents adapter.
#
#   docker build -t saa-livekit-agent .
#   docker run --rm --env-file .env saa-livekit-agent dev    # dev mode
#   docker run --rm --env-file .env saa-livekit-agent start  # production

# Production note: pin `python:3.12-slim` to a specific patch + Debian
# release before deploying (e.g. `python:3.12-slim-bookworm` or a
# digest). `python:3.12-slim` is a moving tag that tracks both Python
# patch releases and Debian base updates; pinning is the reproducibility
# guarantee. Same advice in the other reference Dockerfiles.
FROM python:3.12-slim AS base

# System deps required by the underlying SDKs:
#   - libportaudio2 / portaudio19-dev: sounddevice (mic capture in attenlabs-saa)
#   - libgl1 / libglib2.0-0: opencv-python (camera capture in attenlabs-saa)
#   - ffmpeg: many livekit-agents plugins shell out to ffmpeg for resampling
#   - libsndfile1: silero VAD wheel
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

WORKDIR /app

# Install Python deps first so the layer cache stays warm across code edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py saa_gate.py ./

# LiveKit Agents downloads model files (e.g. Silero VAD) at first run;
# pre-warm the cache during the image build so cold-start in production
# is fast. Falls back to no-op if the model server is unreachable here.
RUN python agent.py download-files || true

# Default to the worker `start` subcommand. Override via `docker run` for
# `dev`, `console`, etc.
ENTRYPOINT ["python", "agent.py"]
CMD ["start"]
