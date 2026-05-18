PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip
PORT   ?= 8765
INSTRUCTIONS ?= Welcome. I'm an AI assistant on this call.

.DEFAULT_GOAL := help

.PHONY: help install dev console test-shape trigger clean

help:  ## Print available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## pip install parent + overlay deps
	$(PIP) install -r ../../livekit/requirements.txt
	$(PIP) install -r requirements.txt

dev:  ## Run the proactive LiveKit worker (dev mode)
	$(PYTHON) proactive_agent.py dev

console:  ## Run in console mode (no LiveKit room)
	$(PYTHON) proactive_agent.py console

test-shape:  ## Offline shape test (no cloud account)
	$(PYTHON) test_smoke_shape.py

trigger:  ## Fire a proactive turn via HTTP (INSTRUCTIONS=... make trigger)
	curl -sS -X POST http://localhost:$(PORT)/trigger \
		-H 'content-type: application/json' \
		-d '{"instructions": "$(INSTRUCTIONS)"}'
	@echo

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
