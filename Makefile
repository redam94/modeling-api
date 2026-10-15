# modeling-api — developer entry points.  Run `make help` for the list.
# Every target is safe to run from a fresh clone after `make install`.

SHELL := /bin/bash
UV ?= uv
PNPM ?= pnpm
DOCKER ?= docker
COMPOSE ?= $(DOCKER) compose
PY_DIRS := packages plugins conformance scripts images tests
DEVOPS_PY := scripts/dev.py scripts/seed.py scripts/image_pins.py scripts/lock_closure.py \
	scripts/release_check.py
CORPUS_SIZE ?= tiny

VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)
VCS_REF := $(shell git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)
BUILD_DATE := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
IMAGES ?= base-bayes base-ml base-forecast server gcs-shim web
DOCKER_BUILD_ARGS := --build-arg VERSION=$(VERSION) --build-arg VCS_REF=$(VCS_REF) \
	--build-arg BUILD_DATE=$(BUILD_DATE)

.DEFAULT_GOAL := help

## ---------------------------------------------------------------- setup
.PHONY: install
install: ## Install all Python workspace packages + dev tools (and JS deps if apps/ exist)
	$(UV) sync --all-packages
	@if [ -f pnpm-workspace.yaml ] && [ -d apps ]; then $(PNPM) install; fi

## ---------------------------------------------------------------- local stack (no Docker)
.PHONY: dev
dev: ## Run the whole stack locally: pgserver Postgres, GCS shim, migrations, api, worker, web, plots
	$(UV) run python scripts/dev.py up $(if $(ONLY),--only $(ONLY)) $(if $(WITHOUT),--without $(WITHOUT))

.PHONY: run
run: dev ## Alias of `make dev`

.PHONY: stop
stop: ## Stop a `make dev` stack running in another shell (and its embedded Postgres)
	$(UV) run python scripts/dev.py stop

.PHONY: status
status: ## Show which local services are up
	$(UV) run python scripts/dev.py status

.PHONY: migrate
migrate: ## Apply database migrations once (starts embedded Postgres if needed)
	$(UV) run python scripts/dev.py migrate

.PHONY: db-shell
db-shell: ## Open psql on the dev database (embedded pgserver or DATABASE_URL)
	$(UV) run python scripts/dev.py db-shell

.PHONY: seed
seed: ## Load the E33 demo datasets + spec into a running stack through the HTTP API
	$(UV) run python scripts/seed.py $(if $(SIZE),--size $(SIZE))

.PHONY: clean-local
clean-local: ## Delete local state in MAPI_DATA_DIR (.local: database, objects); YES=1 skips the prompt
	$(UV) run python scripts/dev.py clean $(if $(YES),--yes)

## ---------------------------------------------------------------- quality
.PHONY: lint
lint: ## Ruff lint + import-linter contracts
	$(UV) run ruff check $(PY_DIRS)
	$(UV) run ruff format --check $(PY_DIRS)
	$(UV) run lint-imports

.PHONY: fmt
fmt: ## Auto-format Python code
	$(UV) run ruff check --fix $(PY_DIRS)
	$(UV) run ruff format $(PY_DIRS)

# Packages checked with ``mypy --strict`` one at a time (per-package strictness is part of
# their contract: e.g. untyped third-party calls need explicit ignores only under --strict).
STRICT_PY := packages/spec/src packages/gcs-shim/src packages/runner/src packages/server/src \
	packages/client/src packages/cli/src

.PHONY: typecheck
typecheck: ## mypy over every type-checked package, the dev scripts and the image helpers
	$(UV) run mypy packages/spec/src packages/sdk/src packages/steps-core/src packages/sdk-pymc/src \
		packages/sdk-sklearn/src packages/plots-core/src plugins/mmm/src plugins/ts/src \
		plugins/tabular/src conformance/src
	@set -e; for pkg in $(STRICT_PY); do echo "mypy --strict $$pkg"; $(UV) run mypy --strict $$pkg; done
	$(UV) run mypy $(DEVOPS_PY)
	$(UV) run mypy images/common

## ---------------------------------------------------------------- tests
.PHONY: test
test: ## Run the default local test suite (no docker/cloud/e2e)
	$(UV) run pytest -q

.PHONY: test-fast
test-fast: ## Unit tests only, skipping slow MCMC tests
	$(UV) run pytest -q -m "not slow and not docker and not cloud and not e2e"

.PHONY: test-all
test-all: test web-test ## Python suite + web (Vitest) suites

.PHONY: e2e
e2e: ## End-to-end tests against a running stack (`make dev` or `make compose-up` first)
	$(UV) run pytest -q -m e2e
	@if ls apps/web/playwright.config.* >/dev/null 2>&1; then \
		$(PNPM) --filter @mapi/web exec playwright test; \
	else echo "e2e: apps/web has no Playwright config; browser suite skipped"; fi

.PHONY: conformance
conformance: ## Run the conformance corpus at tiny size (FIXTURE=<id> picks one; CORPUS_SIZE overrides)
	$(UV) run python -m mapi_conformance run --size $(CORPUS_SIZE) $(if $(FIXTURE),--fixture $(FIXTURE))

.PHONY: conformance-full
conformance-full: ## Run the conformance corpus at full size (nightly-scale data and fits; slow)
	$(UV) run python -m mapi_conformance run --size full $(if $(FIXTURE),--fixture $(FIXTURE))

## ---------------------------------------------------------------- web
.PHONY: web-build
web-build: _web-check ## Production builds of apps/web and apps/plot-sandbox (Vite)
	$(PNPM) --filter @mapi/web build
	$(PNPM) --filter @mapi/plot-sandbox build

.PHONY: web-test
web-test: _web-check ## Vitest suites of apps/web and apps/plot-sandbox
	$(PNPM) --filter @mapi/web --filter @mapi/plot-sandbox run test

.PHONY: _web-check
_web-check:
	@if [ ! -f pnpm-workspace.yaml ] || [ ! -d apps/web ]; then \
		echo "error: the pnpm workspace (pnpm-workspace.yaml, apps/web) is not in this tree yet" >&2; \
		exit 1; fi
	@if [ ! -d node_modules ]; then echo "error: run 'pnpm install' first" >&2; exit 1; fi

.PHONY: web-dev
web-dev: ## Run the web app (5173) and the plot sandbox (5174) against VITE_API_BASE
	$(PNPM) -r --parallel dev

.PHONY: web-check
web-check: ## Lint, typecheck, test (Vitest + Playwright smoke) and build apps/*
	$(UV) run python apps/web/scripts/gen_openapi.py --check
	$(PNPM) -r lint
	$(PNPM) -r typecheck
	$(PNPM) -r test
	$(PNPM) -r build

## ---------------------------------------------------------------- containers
.PHONY: docker-build
docker-build: _docker-check ## Build every image as mapi/<name>:$(VERSION) (IMAGES="server web" for a subset)
	@set -e; for img in $(IMAGES); do \
		echo "==> mapi/$$img:$(VERSION)"; \
		$(DOCKER) build -f images/$$img/Dockerfile $(DOCKER_BUILD_ARGS) -t mapi/$$img:$(VERSION) . ; \
	done
	@if [[ " $(IMAGES) " == *" base-bayes "* ]]; then \
		echo "==> mapi/worker:$(VERSION)"; \
		$(DOCKER) build -f images/base-bayes/Dockerfile --target worker $(DOCKER_BUILD_ARGS) \
			-t mapi/worker:$(VERSION) . ; fi

.PHONY: docker-pin
docker-pin: ## Re-resolve every base-image tag to its current digest (review the diff)
	$(UV) run python scripts/image_pins.py update

.PHONY: docker-pin-check
docker-pin-check: ## Fail if any base or compose image is not pinned by digest
	$(UV) run python scripts/image_pins.py check

.PHONY: compose-up
compose-up: _docker-check _env-check ## Start the docker-compose stack and wait until it is healthy
	MAPI_VERSION=$(VERSION) $(COMPOSE) up -d --wait

.PHONY: compose-down
compose-down: ## Stop the compose stack (VOLUMES=1 also deletes the database and object volumes)
	MAPI_VERSION=$(VERSION) $(COMPOSE) down $(if $(VOLUMES),--volumes)

.PHONY: compose-logs
compose-logs: ## Follow compose logs (SERVICE=api for one service)
	MAPI_VERSION=$(VERSION) $(COMPOSE) logs -f --tail=200 $(SERVICE)

.PHONY: _docker-check
_docker-check:
	@$(DOCKER) info >/dev/null 2>&1 || { \
		echo "error: the Docker daemon is not reachable ('docker info' failed)." >&2; \
		echo "       Without Docker, use 'make dev' (pgserver + local processes)." >&2; exit 1; }

.PHONY: _env-check
_env-check:
	@if [ ! -f .env ]; then \
		echo "error: no .env - run 'cp .env.example .env' and set POSTGRES_PASSWORD" >&2; exit 1; fi

## ---------------------------------------------------------------- docs
.PHONY: docs-catalog
docs-catalog: ## Regenerate the catalog reference pages (steps, models, plots, spec schema, errors)
	$(UV) run python scripts/gen_docs_catalog.py

.PHONY: docs-serve
docs-serve: docs-catalog ## Serve the documentation site with live reload
	$(UV) run --group docs mkdocs serve

.PHONY: docs-build
docs-build: docs-catalog ## Build the documentation site into ./site (strict)
	$(UV) run --group docs mkdocs build --strict

.PHONY: docs-test
docs-test: ## Run every documentation snippet and check every include
	$(UV) run pytest -q conformance/tests/test_docs_snippets.py

## ---------------------------------------------------------------- release
.PHONY: release-check
release-check: ## One version everywhere, a dated CHANGELOG entry, no plan files tracked, pinned images
	$(UV) run python scripts/release_check.py
	$(UV) run python scripts/image_pins.py check

## ---------------------------------------------------------------- housekeeping
.PHONY: clean
clean: ## Remove caches and build output (keeps .venv and .local)
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov site dist build
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage: make <target>\n"} \
	  /^## -+ / {h = $$0; sub(/^## -+ /, "", h); printf "\n\033[1m%s\033[0m\n", h} \
	  /^[a-zA-Z0-9][a-zA-Z0-9_.-]*:.*##/ {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
