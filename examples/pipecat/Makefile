# SAA × Pipecat reference adapter, dev loop.
.PHONY: help install install-overlay dev run cloud smoke test test-shape lint overlay docker clean

PYTHON ?= python3
TRANSPORT ?= daily
OVERLAY_PORT ?= 8080
IMAGE ?= saa-pipecat

.DEFAULT_GOAL := help

help:  ## Print available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## pip install -r requirements.txt
	$(PYTHON) -m pip install -r requirements.txt

install-overlay:  ## pip install overlay shim extras
	$(PYTHON) -m pip install -r requirements-overlay.txt

dev:  ## Run the local-audio bot (laptop mode)
	$(PYTHON) bot.py

run: dev  ## Alias for `make dev`

cloud:  ## Run against a cloud transport: `make cloud TRANSPORT=daily|smallwebrtc|twilio`
	pipecat-runner --transport $(TRANSPORT) bot:bot

overlay:  ## Run bot + SSE overlay shim together (open http://localhost:8080)
	SAA_OVERLAY_PORT=$(OVERLAY_PORT) $(PYTHON) -c 'import asyncio; from overlay_server import run_with_overlay; asyncio.run(run_with_overlay())'

test-shape:  ## No-network shape check (CI uses this)
	$(PYTHON) test_smoke_shape.py

smoke: test-shape  ## Alias for `make test-shape`

test:  ## Full pytest suite (needs pipecat-ai installed)
	$(PYTHON) -m pytest tests -q

lint:  ## Ruff over the example files
	$(PYTHON) -m ruff check saa_gate.py bot.py overlay_server.py tests test_smoke_shape.py

docker:  ## Build the reference Docker image
	docker build -t $(IMAGE) .

clean:  ## Remove caches
	rm -rf __pycache__ tests/__pycache__
