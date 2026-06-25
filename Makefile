.DEFAULT_GOAL := help
.PHONY: help build test test-js test-py clean

help:  ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

build:  ## Install + build the @attenlabs/saa-js SDK
	npm install --no-save --prefix packages/saa-js
	npm run build --prefix packages/saa-js

test: test-js test-py  ## Run every JS + Python suite (mirrors CI)

test-js:  ## Run the JS test suite
	npm test --prefix packages/saa-js

test-py:  ## Run the Python test suite (exit 5 = no tests collected; tolerated during migration)
	python -m pytest packages/saa-py || [ $$? -eq 5 ]

clean:  ## Remove the JS build output
	npm run clean --prefix packages/saa-js