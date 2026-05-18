# syntax=docker/dockerfile:1.6
# Reference Dockerfile for the SAA × Twilio Media Streams adapter.
#
#   docker build -t saa-twilio .
#   docker run --rm -p 8000:8000 --env-file .env saa-twilio
#
# The image runs the FastAPI server with /voice (TwiML) and /twilio
# (WebSocket) endpoints. For high-volume telephony, front it with a
# process manager (gunicorn + uvicorn workers) and a connection-aware
# load balancer; one process per CPU is a reasonable starting point.

# Production note: pin `python:3.12-slim` to a specific patch + Debian
# release before deploying (e.g. `python:3.12-slim-bookworm` or a
# digest). `python:3.12-slim` is a moving tag that tracks both Python
# patch releases and Debian base updates; pinning is the reproducibility
# guarantee. Same advice in the other reference Dockerfiles.
FROM python:3.12-slim AS base

# numpy / cryptography wheels expect a few system libraries on slim.
RUN apt-get update && apt-get install -y --no-install-recommends \
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

COPY *.py ./

EXPOSE 8000

ENV HOST=0.0.0.0 \
    PORT=8000 \
    LOG_LEVEL=info

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://localhost:'+os.environ.get('PORT','8000')+'/health',timeout=2).status==200 else 1)"

USER appuser

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
