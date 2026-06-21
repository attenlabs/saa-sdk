.DEFAULT_GOAL := help
.PHONY: help build test test-js test-py clean

help:  ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

build:  ## Install + build the @attenlabs/saa-js SDK
	npm install --no-save -w @attenlabs/saa-js
	npm run build:js

test: test-js test-py  ## Run every JS + Python suite (mirrors CI)