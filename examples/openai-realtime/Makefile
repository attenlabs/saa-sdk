# SAA × OpenAI Realtime, make targets.
#
# Run `make` (no args) to print this help. The Dockerfile and
# requirements.txt do the heavy lifting; each target is a one-liner.

PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip
IMAGE  ?= saa-openai-realtime
PORT   ?= 8080
NGROK_REGION ?= us

.DEFAULT_GOAL := help

.PHONY: help install dev run static lint test test-shape \
        docker docker-run docker-dev tunnel clean

help:  ## Print available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:  ## pip install -r requirements.txt
	$(PIP) install -r requirements.txt

dev:  ## Run the relay with auto-reload (default)
	$(PYTHON) -m uvicorn server:app --host 0.0.0.0 --port $(PORT) --reload

run:  ## Run the relay (production-style, no auto-reload)
	$(PYTHON) -m uvicorn server:app --host 0.0.0.0 --port $(PORT)

static:  ## Serve only the browser bundle on $(PORT) (no relay)
	$(PYTHON) -m http.server -d . $(PORT)

tunnel:  ## Open an ngrok tunnel so Twilio can reach /twilio (requires ngrok)
	@command -v ngrok >/dev/null 2>&1 || { echo "ngrok not installed; brew install ngrok or see ngrok.com/download"; exit 1; }
	ngrok http --region=$(NGROK_REGION) $(PORT)

lint:  ## Lint with ruff (skipped if ruff isn't installed)
	@ruff --version >/dev/null 2>&1 && ruff check . || echo "ruff not installed; skip"

test:  ## Run pytest if installed; otherwise the no-network smoke tests
	@$(PYTHON) -m pytest --version >/dev/null 2>&1 \
		&& $(PYTHON) -m pytest -q \
		|| $(MAKE) test-shape

test-shape:  ## Run no-network smoke tests for both server.py and main.js
	$(PYTHON) test_smoke_shape.py
	@command -v node >/dev/null 2>&1 && node test_smoke_shape.mjs || echo "node not installed; skip mjs check"

docker:  ## Build the Docker image
	docker build -t $(IMAGE) .

docker-run:  ## Run the relay inside Docker (mapping :$(PORT))
	docker run --rm -p $(PORT):$(PORT) --env-file .env $(IMAGE)

docker-dev:  ## Run the relay inside Docker with the local source mounted
	docker run --rm -p $(PORT):$(PORT) --env-file .env -v $(PWD):/app $(IMAGE)

clean:  ## Remove caches
	rm -rf __pycache__ .pytest_cache .ruff_cache *.pyc
