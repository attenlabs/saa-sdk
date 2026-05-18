PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip
PORT   ?= 8765
TRANSPORT ?= daily
INSTRUCTIONS ?= Action item from the standup: draft the design doc by EOD. Want me to start a first pass?

.DEFAULT_GOAL := help

.PHONY: help install dev test-shape trigger clean

help:  ## Print available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## pip install -r requirements.txt (also pull parent deps)
	$(PIP) install -r ../../pipecat/requirements.txt
	$(PIP) install -r requirements.txt

dev:  ## Run the proactive bot via pipecat-runner
	pipecat-runner --transport $(TRANSPORT) proactive_bot:bot

test-shape:  ## Offline shape test (no cloud account)
	$(PYTHON) test_smoke_shape.py

trigger:  ## Fire a proactive turn via HTTP (INSTRUCTIONS=... make trigger)
	curl -sS -X POST http://localhost:$(PORT)/trigger \
		-H 'content-type: application/json' \
		-d '{"instructions": "$(INSTRUCTIONS)"}'
	@echo

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
