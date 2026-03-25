SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

UV ?= uv
PYTHON ?= .venv/bin/python
PYTEST ?= .venv/bin/pytest
RUFF ?= .venv/bin/ruff
NPM ?= npm
WEB_DIR := apps/web

.PHONY: help setup db-start db-stop db-status migrate api web web-install test test-api lint check backfill-integrity outbox-relay

help: ## Show available commands
	@awk 'BEGIN {FS = ":.*## "; print "Available targets:"} /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install Python dependencies with uv
	$(UV) sync --dev

db-start: ## Start local PostgreSQL (port 55432)
	bash scripts/db-start.sh

db-stop: ## Stop local PostgreSQL
	bash scripts/db-stop.sh

db-status: ## Check local PostgreSQL status
	bash scripts/db-status.sh

migrate: ## Apply SQL migrations
	bash scripts/migrate.sh

api: ## Start FastAPI service (apps/api)
	bash scripts/api-start.sh

web-install: ## Install frontend dependencies (apps/web)
	cd $(WEB_DIR) && $(NPM) install

web: ## Start React dashboard (apps/web)
	bash scripts/dashboard-start.sh

test: ## Run all pytest tests
	@if [[ -f .env ]]; then set -a; source .env; set +a; fi; $(PYTEST) -q

test-api: ## Run API integration tests only
	@if [[ -f .env ]]; then set -a; source .env; set +a; fi; $(PYTEST) -q tests/test_api_auth.py tests/test_api_surface.py

lint: ## Run Ruff checks for backend code and tests
	$(RUFF) check apps/api src tests

check: lint test ## Run lint and tests

backfill-integrity: ## Backfill integrity metadata (dry-run). Example: make backfill-integrity ARGS="--apply --mode missing"
	$(PYTHON) scripts/backfill_integrity_hashes.py $(ARGS)

outbox-relay: ## Run outbox relay worker once. Example: make outbox-relay ARGS="--once"
	$(PYTHON) scripts/run_outbox_relay.py $(ARGS)
