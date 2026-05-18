# syntax=docker/dockerfile:1.6
# Reference Dockerfile for the SAA × Pipecat adapter.
#
#   docker build -t saa-pipecat .
#   docker run --rm --env-file .env saa-pipecat
#
# Builds on the official Daily / Pipecat base image so the runtime matches
# what Pipecat Cloud uses. The image ships both the gate/bot and the
# overlay SSE shim; run either via the corresponding CMD.
#
# Production note: pin `dailyco/pipecat-base` to a specific tag (e.g.
# `:0.0.86`) before deploying. `:latest` tracks the upstream Pipecat
# release rhythm and changes without a SemVer contract; pinning is the
# reproducibility guarantee. The Pipecat Cloud worker behaves the same
# either way — only the developer-side image needs the pin.

FROM dailyco/pipecat-base:latest

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=0 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt requirements-overlay.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt -r requirements-overlay.txt

COPY saa_gate.py bot.py overlay_server.py ./

# Pipecat-runner picks up `bot:bot` from this entrypoint when the container
# is launched by Pipecat Cloud. Override CMD to ``python overlay_server.py``
# to run the SSE shim instead.
CMD ["python", "bot.py"]
