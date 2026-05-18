# Targets for the proactive AI-SDR example. The example is a thin
# overlay on examples/twilio/; ``main:app`` imports the underlying
# FastAPI app from there and registers ``AISDRBridge`` as the per-call
# bridge factory.

PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip
PORT   ?= 8000
IMAGE  ?= saa-proactive-agent
NGROK_REGION ?= us

.DEFAULT_GOAL := help

.PHONY: help install dev run lint test test-shape tunnel place-call docker docker-run clean

help:  ## Print available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## pip install -r requirements.txt
	$(PIP) install -r requirements.txt

dev:  ## Run the relay with auto-reload
	$(PYTHON) -m uvicorn main:app --host 0.0.0.0 --port $(PORT) --reload

run:  ## Run the relay (production-style, no auto-reload)
	$(PYTHON) -m uvicorn main:app --host 0.0.0.0 --port $(PORT)

tunnel:  ## Open an ngrok tunnel so Twilio can reach /voice (requires ngrok)
	@command -v ngrok >/dev/null 2>&1 || { echo "ngrok not installed; see ngrok.com/download"; exit 1; }
	ngrok http --region=$(NGROK_REGION) $(PORT)

place-call:  ## Place an outbound proactive call (TO=+15551112222 make place-call)
	@test -n "$(TO)" || { echo "usage: TO=+15551112222 make place-call"; exit 2; }
	$(PYTHON) place_proactive_call.py $(TO)

lint:  ## Lint with ruff (skipped if ruff isn't installed)
	@ruff --version >/dev/null 2>&1 && ruff check . || echo "ruff not installed; skip"

test:  ## Run pytest (skipped if not installed)
	@$(PYTHON) -m pytest --version >/dev/null 2>&1 && $(PYTHON) -m pytest -q || echo "pytest not installed; skip"

test-shape:  ## No-network smoke test (CI uses this)
	$(PYTHON) test_smoke_shape.py

docker:  ## Build the Docker image
	docker build -t $(IMAGE) -f Dockerfile ../..

docker-run:  ## Run the Docker image
	docker run --rm -p $(PORT):$(PORT) --env-file .env $(IMAGE)

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
