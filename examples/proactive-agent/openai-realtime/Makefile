PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip
PORT   ?= 8000
INSTRUCTIONS ?= I see your tests are red. Want me to look?

.DEFAULT_GOAL := help

.PHONY: help install dev run test-shape trigger clean

help:  ## Print available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## pip install parent + overlay deps
	$(PIP) install -r ../../openai-realtime/requirements.txt
	$(PIP) install -r requirements.txt

dev:  ## Run the relay with auto-reload (proactive_server:app)
	$(PYTHON) -m uvicorn proactive_server:app --host 0.0.0.0 --port $(PORT) --reload

run:
	$(PYTHON) -m uvicorn proactive_server:app --host 0.0.0.0 --port $(PORT)

test-shape:  ## Offline shape test (no cloud account)
	$(PYTHON) test_smoke_shape.py

trigger:  ## Fire a proactive turn from the CLI (INSTRUCTIONS=... make trigger)
	curl -sS -X POST http://localhost:$(PORT)/proactive-trigger \
		-H 'content-type: application/json' \
		-d '{"instructions": "$(INSTRUCTIONS)"}'
	@echo

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
