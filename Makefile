.DEFAULT_GOAL := help
.PHONY: help build test test-js test-py clean

help:  ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

build:  ## Install + build the @attenlabs/saa-js SDK
	npm install --no-save -w @attenlabs/saa-js
	npm run build:js

test: test-js test-py  ## Run every JS + Python suite (mirrors CI)

test-js: build  ## saa-gate + saa-proactive-js unit + integration tests
	node --test packages/saa-gate/tests/*.test.mjs
	node --test packages/saa-proactive-js/tests/*.test.mjs

test-py:  ## saa-py + saa-proactive-py unit + integration tests
	pip install -e packages/saa-py
	pip install -e packages/saa-proactive-py
	cd packages/saa-py && python -m pytest -q tests
	cd packages/saa-proactive-py && python -m pytest -q tests

clean:  ## Remove node_modules, dist, __pycache__, .pytest_cache
	rm -rf node_modules packages/*/node_modules packages/*/dist
	find . -type d \( -name '__pycache__' -o -name '.pytest_cache' -o -name '*.egg-info' \) -not -path './.git/*' -exec rm -rf {} + 2>/dev/null || true
