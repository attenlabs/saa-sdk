# SAA × LiveKit Agents, make targets.
#
# Run `make` (no args) to print this help. The Dockerfile and
# requirements.txt do the heavy lifting; each target is a one-liner.

PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip
IMAGE  ?= saa-livekit-agent
ROOM   ?= saa-demo

.DEFAULT_GOAL := help

.PHONY: help install dev run console connect download-files \
        serve-token lint test test-shape docker docker-run docker-dev clean

help:  ## Print available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:  ## pip install -r requirements.txt
	$(PIP) install -r requirements.txt

install-serve:  ## pip install -e ".[serve]" (adds fastapi + uvicorn + livekit-api for serve_token.py)
	$(PIP) install -e ".[serve]"

dev:  ## Run the worker in dev mode (watches for file changes)
	$(PYTHON) agent.py dev

run:  ## Run the worker (production-style)
	$(PYTHON) agent.py start

console:  ## Run the agent in console mode (no LiveKit room required)
	$(PYTHON) agent.py console

connect:  ## Join a specific room (e.g. make connect ROOM=demo-1)
	$(PYTHON) agent.py connect --room=$(ROOM)

download-files:  ## Pre-fetch model artefacts (Silero VAD etc.)
	$(PYTHON) agent.py download-files

serve-token:  ## Run the LiveKit JWT mint helper at http://localhost:8088 (requires [serve] extras)
	$(PYTHON) serve_token.py

lint:  ## Lint with ruff (skipped if ruff isn't installed)
	@ruff --version >/dev/null 2>&1 && ruff check . || echo "ruff not installed; skip"

test:  ## Run pytest if installed; otherwise the no-network smoke tests
	@$(PYTHON) -m pytest --version >/dev/null 2>&1 \
		&& $(PYTHON) -m pytest -q \
		|| $(MAKE) test-shape

test-shape:  ## Run the no-network smoke test (CI uses this)
	$(PYTHON) test_smoke_shape.py

docker:  ## Build the Docker image
	docker build -t $(IMAGE) .

docker-run:  ## Run the worker (start) inside Docker
	docker run --rm --env-file .env $(IMAGE) start

docker-dev:  ## Run the worker (dev) inside Docker
	docker run --rm --env-file .env $(IMAGE) dev

clean:  ## Remove caches
	rm -rf __pycache__ .pytest_cache .ruff_cache *.pyc tests/__pycache__
