# syntax=docker/dockerfile:1.6
# Reference Dockerfile for the SAA × OpenAI Realtime relay.
#
#   docker build -t saa-openai-realtime .
#   docker run --rm -p 8080:8080 --env-file .env saa-openai-realtime
#
# The image bundles the FastAPI relay (ephemeral-token mint + Twilio
# bridge) AND the static browser bundle, so a single container serves
# both `/session` (mint) and `/` (demo) behind one origin.

# Production note: pin `python:3.12-slim` to a specific patch + Debian
# release before deploying (e.g. `python:3.12-slim-bookworm` or a
# digest). `python:3.12-slim` is a moving tag that tracks both Python
# patch releases and Debian base updates; pinning is the reproducibility
# guarantee. Same advice in the other reference Dockerfiles.
FROM python:3.12-slim AS base

# audioop, opencv, sounddevice all need a few system libraries even
# though this relay runs `enable_audio=False, enable_video=False` on the
# SAA client. Without them, the Python imports fail at boot.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libportaudio2 \
        libgl1 \
        libglib2.0-0 \
        libsndfile1 \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. Created here so all subsequent COPY/RUN steps
# happen as root (for /usr/local/lib pip installs) but the final CMD
# runs unprivileged.
RUN groupadd -r appuser && useradd --no-log-init -r -g appuser appuser

WORKDIR /app

# Install Python deps first so the layer cache stays warm across code edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app + the browser bundle that StaticFiles serves at /. Smoke
# tests are NOT copied, they only matter at build time.
COPY server.py index.html main.js ./

EXPOSE 8080

ENV HOST=0.0.0.0 \
    PORT=8080 \
    LOG_LEVEL=info

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://localhost:'+os.environ.get('PORT','8080')+'/health',timeout=2).status==200 else 1)"

USER appuser

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
