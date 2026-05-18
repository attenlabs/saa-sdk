# syntax=docker/dockerfile:1.6
# Reference Dockerfile for the SAA proactive Pipecat overlay.
#
# Built from the parent ``examples/`` directory so the image can include
# both ``pipecat/`` (the underlying adapter) and
# ``proactive-agent/pipecat/`` (this overlay). Build from
# ``examples/proactive-agent/pipecat/`` with::
#
#   docker build -t saa-proactive-agent-pipecat -f Dockerfile ../..
#   docker run --rm --env-file .env -p 8765:8765 saa-proactive-agent-pipecat
#
# Uses the official Daily / Pipecat base image so the runtime matches
# Pipecat Cloud. The image runs ``proactive_bot:bot`` via pipecat-runner;
# the proactive HTTP sidecar listens on PROACTIVE_HTTP_PORT (default
# 8765).
#
# Production note: pin `dailyco/pipecat-base` to a specific tag (e.g.
# `:0.0.86`) before deploying. `:latest` tracks the upstream Pipecat
# release rhythm and changes without a SemVer contract; pinning is the
# reproducibility guarantee.

FROM dailyco/pipecat-base:latest

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=0 \
    PYTHONUNBUFFERED=1 \
    PROACTIVE_HTTP_PORT=8765

WORKDIR /app

# Layer 1: parent's runtime + overlay deps. COPY each separately so
# basename collisions don't drop a file.
COPY pipecat/requirements.txt ./parent-requirements.txt
COPY pipecat/requirements-overlay.txt ./parent-overlay-requirements.txt
COPY proactive-agent/pipecat/requirements.txt ./overlay-requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
        -r ./parent-requirements.txt \
        -r ./parent-overlay-requirements.txt \
        -r ./overlay-requirements.txt

# Layer 2: parent adapter + overlay.
COPY pipecat/ ./pipecat/
COPY proactive-agent/pipecat/ ./proactive-agent/pipecat/

WORKDIR /app/proactive-agent/pipecat

EXPOSE 8765

CMD ["pipecat-runner", "--transport", "daily", "proactive_bot:bot"]
