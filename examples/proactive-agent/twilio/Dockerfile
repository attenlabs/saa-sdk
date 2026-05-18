# syntax=docker/dockerfile:1.6
# Reference Dockerfile for the SAA proactive AI-SDR overlay (Twilio variant).
#
# Built from the parent ``examples/`` directory so the image can include
# both ``twilio/`` (the underlying adapter) and ``proactive-agent/twilio/``
# (this overlay). Build from ``examples/proactive-agent/twilio/`` with::
#
#   docker build -t saa-proactive-agent-twilio -f Dockerfile ../..
#   docker run --rm -p 8000:8000 --env-file .env saa-proactive-agent-twilio
#
# The image runs ``main:app``, which imports the FastAPI app from
# ``examples/twilio/server.py`` and registers ``AISDRBridge`` as the
# per-call bridge factory.

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

# Install Python deps first so the layer cache stays warm across edits.
COPY proactive-agent/twilio/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the underlying Twilio adapter AND the proactive overlay. The
# overlay's main.py adds ../../twilio to sys.path, so at runtime
# /app/twilio/ and /app/proactive-agent/twilio/ must coexist.
COPY twilio/ ./twilio/
COPY proactive-agent/twilio/ ./proactive-agent/twilio/

WORKDIR /app/proactive-agent/twilio

EXPOSE 8000

ENV HOST=0.0.0.0 \
    PORT=8000 \
    LOG_LEVEL=info

# The /health route is inherited from the parent examples/twilio/server.py
# app object that main.py imports and extends.
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://localhost:'+os.environ.get('PORT','8000')+'/health',timeout=2).status==200 else 1)"

USER appuser

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
