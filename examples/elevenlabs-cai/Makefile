# SAA × ElevenLabs Conversational AI, make targets.
#
# `make` (no args) prints help. One FastAPI process serves three roles:
#
#   GET  /                              → the browser bundle
#   GET  /api/conversation-config       → agentId / transport / gate / threshold
#   GET  /api/conversation-token        → short-lived WebRTC token
#   GET  /api/signed-url                → signed WebSocket URL fallback
#   GET  /health                        → server config sanity
#   WS   /twilio                        → PSTN ↔ SAA gate ↔ ElevenLabs CAI

PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip
HOST   ?= 0.0.0.0
PORT   ?= 8080
IMAGE  ?= saa-elevenlabs-cai
NGROK_REGION ?= us

.DEFAULT_GOAL := help

.PHONY: help install dev run test test-shape lint tunnel docker docker-run clean

help:  ## Print available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## pip install -r requirements.txt
	$(PIP) install -r requirements.txt

dev:  ## uvicorn with auto-reload (browser + token-mint + relay on $(PORT))
	RELOAD=1 $(PYTHON) -m uvicorn server:app --host $(HOST) --port $(PORT) --reload

run:  ## uvicorn in production mode (no reload)
	$(PYTHON) -m uvicorn server:app --host $(HOST) --port $(PORT)

tunnel:  ## Open an ngrok tunnel on $(PORT) so Twilio can reach /twilio
	@command -v ngrok >/dev/null 2>&1 || { echo "ngrok not installed; brew install ngrok or see ngrok.com/download"; exit 1; }
	ngrok http --region=$(NGROK_REGION) $(PORT)

lint:  ## ruff (skipped if not installed)
	@ruff --version >/dev/null 2>&1 && ruff check . || echo "ruff not installed; skip"

test:  ## pytest if installed; otherwise the no-network smoke tests
	@$(PYTHON) -m pytest --version >/dev/null 2>&1 \
		&& $(PYTHON) -m pytest -q \
		|| $(MAKE) test-shape

test-shape:  ## No-network shape checks for the relay + the browser bundle
	$(PYTHON) test_smoke_shape.py
	@command -v node >/dev/null 2>&1 && node --test test_smoke_shape.mjs || echo "node not installed; skip mjs check"

docker:  ## Build the Docker image
	docker build -t $(IMAGE) .

docker-run:  ## docker run --env-file .env -p $(PORT):$(PORT) $(IMAGE)
	docker run --rm -p $(PORT):$(PORT) --env-file .env $(IMAGE)

clean:  ## Drop caches
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .ruff_cache .pytest_cache
