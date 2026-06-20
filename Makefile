.DEFAULT_GOAL := help
.PHONY: help build check clean

help:  ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-8s\033[0m %s\n", $$1, $$2}'

build:  ## Build the @attenlabs/saa-js SDK
	cd packages/saa-js && npm install && npm run build

check:  ## Build saa-js and build wheels for the Python packages (no tests yet)
	cd packages/saa-js && npm install && npm run build
	python -m pip install --quiet --upgrade build
	for p in saa-py saa-livekit-client saa-pipecat-client; do python -m build --wheel packages/$$p; done

clean:  ## Remove build artifacts
	rm -rf packages/saa-js/node_modules packages/saa-js/dist packages/*/dist
	find . -type d \( -name '__pycache__' -o -name '*.egg-info' \) -not -path './.git/*' -exec rm -rf {} + 2>/dev/null || true
