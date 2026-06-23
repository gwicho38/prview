.DEFAULT_GOAL := help
IMAGE := prview:dev

.PHONY: help install test run docker-build docker-run clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Sync dependencies (incl. dev group) into the uv venv
	uv sync

test: ## Run the full test suite
	uv run pytest -q

run: ## Launch prview (free localhost port, opens the browser)
	uv run prview

docker-build: ## Build the container image
	docker build -t $(IMAGE) .

docker-run: ## Run the container (binds 127.0.0.1:8000; needs gh/claude for live use)
	docker run --rm -p 127.0.0.1:8000:8000 $(IMAGE)

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache **/__pycache__ dist build
