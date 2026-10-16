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
# Only a SIZE given on the make command line counts: toolchains (conda's compilers, for one)
# export SIZE=<path of the binutils `size` tool> into the environment.
seed: ## Load the E33 demo datasets + spec into a running stack (`make seed SIZE=full`)
	$(UV) run python scripts/seed.py $(if $(filter command line,$(origin SIZE)),--size $(SIZE))

.PHONY: outbox
outbox: ## Show the dev stack's newest e-mails (sign-up verification links; TO=<address> filters)
	$(UV) run python scripts/dev.py outbox $(if $(TO),--to $(TO))

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
STRICT_PY := packages/spec/src packages/sdk/src packages/gcs-shim/src packages/runner/src packages/server/src \
	packages/workflows/src \
	packages/client/src packages/cli/src

.PHONY: typecheck
typecheck: ## mypy over every type-checked package, the dev scripts and the image helpers
	$(UV) run mypy packages/spec/src packages/sdk/src packages/steps-core/src packages/sdk-pymc/src \
		packages/sdk-sklearn/src packages/plots-core/src plugins/mmm/src plugins/ts/src \
		plugins/tabular/src plugins/statsmodels/src conformance/src
	@set -e; for pkg in $(STRICT_PY); do echo "mypy --strict $$pkg"; $(UV) run mypy --strict $$pkg; done
	$(UV) run mypy $(DEVOPS_PY)
	$(UV) run mypy images/common

# Downloaded Terraform providers are kept here between `make tf-validate` runs (git-ignored).
TF_PLUGIN_CACHE ?= $(CURDIR)/.local/terraform-plugins

.PHONY: tf-validate
tf-validate: ## Terraform fmt + validate (every env) + mocked-provider module tests; never applies
	@mkdir -p $(TF_PLUGIN_CACHE)
	TF_PLUGIN_CACHE_DIR=$(TF_PLUGIN_CACHE) $(UV) run pytest -q tests/infra

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
e2e: ## End-to-end tests against a running stack (`make dev` first): API suites (M1-M3), then the browser @e2e suites
	$(UV) run pytest -q -m e2e
	@if ls apps/web/playwright.config.* >/dev/null 2>&1; then \
		$(PNPM) --filter @mapi/web e2e; \
	else echo "e2e: apps/web has no Playwright config; browser suite skipped"; fi

.PHONY: chaos
chaos: ## Chaos tests: worker killed mid-run, event redelivery/reordering, Postgres restart
	$(UV) run pytest -q packages/server/tests/test_chaos.py

.PHONY: load-test
load-test: ## 100 concurrent fits through dispatcher/reconciler/finalizer on fake Cloud Run (LOAD_TEST_RUNS, LOAD_TEST_FIT_MS)
	$(UV) run pytest -q -s packages/server/tests/test_load_cloudrun.py

.PHONY: conformance
conformance: ## Run the conformance corpus at tiny size (FIXTURE=<id> picks one; CORPUS_SIZE overrides)
	$(UV) run python -m mapi_conformance run --size $(CORPUS_SIZE) $(if $(FIXTURE),--fixture $(FIXTURE))

.PHONY: conformance-full
conformance-full: ## Run the conformance corpus at full size (nightly-scale data and fits; slow)
	$(UV) run python -m mapi_conformance run --size full $(if $(FIXTURE),--fixture $(FIXTURE))

## ---------------------------------------------------------------- security
# The security suite lives in security/ (NOT a uv workspace member, so never installed by
# `uv sync`). It runs against the installed venv with security/src on PYTHONPATH. EXECUTOR
# selects the isolation level (local-process|docker|docker-gvisor|cloud); docker executors
# auto-skip their sandbox probes when no Docker daemon is present.
SECURITY_PYTHONPATH := security/src:security/plugin/src
EXECUTOR ?= local-process

.PHONY: security-suite
security-suite: ## Run the sandbox-escape probe suite (EXECUTOR=docker-gvisor for the gVisor run)
	PYTHONPATH=$(SECURITY_PYTHONPATH) $(UV) run python -m mapi_security run --executor $(EXECUTOR) \
		--metrics .local/security-$(EXECUTOR).prom
	PYTHONPATH=$(SECURITY_PYTHONPATH) $(UV) run pytest -q security/tests -m sandbox

.PHONY: security-list
security-list: ## List the security probe corpus
	PYTHONPATH=$(SECURITY_PYTHONPATH) $(UV) run python -m mapi_security list

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

.PHONY: api-types
api-types: ## Regenerate apps/web/openapi.json, access.gen.json and src/api/schema.d.ts from the server
	$(UV) run python apps/web/scripts/gen_openapi.py
	$(PNPM) --filter @mapi/web openapi:types

.PHONY: snapshots
snapshots: ## Regenerate the contract snapshot (mapi_spec), the server's Guided catalog fixture and the web mock fixtures
	MAPI_UPDATE_SNAPSHOTS=1 $(UV) run pytest -q packages/spec/tests/test_contract_snapshot.py \
		packages/spec/tests/test_m3_whatif_golden.py \
		conformance/tests/test_guided_catalog.py
	$(UV) run python apps/web/scripts/gen_guided_fixtures.py
	$(UV) run python apps/web/scripts/gen_integration_fixtures.py
	$(UV) run python apps/web/scripts/gen_explorer_fixtures.py
	$(UV) run python apps/web/scripts/gen_workflow_fixtures.py

.PHONY: web-check
web-check: ## Lint, typecheck, test (Vitest + Playwright smoke) and build apps/*
	$(UV) run python apps/web/scripts/gen_openapi.py --check
	$(UV) run python apps/web/scripts/gen_guided_fixtures.py --check
	$(UV) run python apps/web/scripts/gen_integration_fixtures.py --check
	$(UV) run python apps/web/scripts/gen_explorer_fixtures.py --check
	$(UV) run python apps/web/scripts/gen_workflow_fixtures.py --check
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
docs-catalog: ## Regenerate the generated reference pages (catalogs, errors, CLI, settings, OpenAPI, permissions, metrics)
	$(UV) run python scripts/gen_docs_catalog.py
	$(UV) run python scripts/gen_docs_reference.py
	$(UV) run python scripts/gen_docs_platform.py

.PHONY: docs-serve
docs-serve: docs-catalog ## Serve the documentation site with live reload
	$(UV) run --group docs mkdocs serve

.PHONY: docs-build
docs-build: docs-catalog ## Build the documentation site into ./site (strict)
	$(UV) run --group docs mkdocs build --strict

.PHONY: docs-test
docs-test: ## Run every documentation snippet and check every include
	$(UV) run pytest -q conformance/tests/test_docs_snippets.py conformance/tests/test_docs_platform.py conformance/tests/test_docs_tutorials.py \
		conformance/tests/test_docs_v05.py packages/server/tests/test_docs_client_v05.py

.PHONY: docs-screenshots
docs-screenshots: ## Recapture docs/assets/screenshots from a running, seeded stack (see the script)
	node scripts/docs_screenshots.mjs .local/screenshots
	$(UV) run python scripts/optimise_screenshots.py .local/screenshots docs/assets/screenshots

DOCS_ADMIN_SHOTS := signin,dev-idp,project-switcher,members,tokens,token-new,token-created,usage,admin-tenants,admin-quotas,admin-kill-switches,admin-executors,admin-audit

.PHONY: docs-screenshots-guided
docs-screenshots-guided: ## Recapture the Guided tutorial screenshots from the mock-mode app (Playwright)
	MAPI_GUIDED_SCREENSHOTS=1 MAPI_SMOKE_WEB_PORT=$${MAPI_SMOKE_WEB_PORT:-15173} MAPI_SMOKE_PLOTS_PORT=$${MAPI_SMOKE_PLOTS_PORT:-15174} \
		$(PNPM) --filter @mapi/web exec playwright test e2e/guided-screenshots.spec.ts
	$(UV) run python scripts/optimise_screenshots.py .local/screenshots-guided docs/assets/screenshots --max-kb 150

.PHONY: docs-screenshots-admin
docs-screenshots-admin: ## Recapture the multi-tenant screenshots (sign-in, members, tokens, usage, admin) from a dev stack
	node scripts/docs_screenshots_admin.mjs .local/screenshots-admin
	$(UV) run python scripts/optimise_screenshots.py .local/screenshots-admin docs/assets/screenshots --only $(DOCS_ADMIN_SHOTS)

DOCS_INTEGRATIONS_SHOTS := integrations-overview,integrations-promote-gate,integrations-endpoint,integrations-try-it,integrations-snippets,integrations-webhook,integrations-sharing,integrations-embed-link,settings-data,admin-signups,signup

.PHONY: docs-screenshots-integrations
docs-screenshots-integrations: ## Recapture the Integrations hub and trust-page screenshots from the mock-mode web app (see the script)
	node scripts/docs_screenshots_integrations.mjs .local/screenshots-integrations
	$(UV) run python scripts/optimise_screenshots.py .local/screenshots-integrations docs/assets/screenshots --max-kb 150 --only $(DOCS_INTEGRATIONS_SHOTS)

DOCS_TUTORIAL_SHOTS := integrations-sheets,webhook-secret,embed-page

.PHONY: docs-screenshots-tutorials
docs-screenshots-tutorials: ## Recapture the v0.4 tutorial screenshots (Sheets card, webhook secret, embed page) from mock mode
	$(UV) run python scripts/docs_embed_view.py .local/embed-view.json
	MAPI_EMBED_VIEW=.local/embed-view.json node scripts/docs_screenshots_tutorials.mjs .local/screenshots-tutorials
	$(UV) run python scripts/optimise_screenshots.py .local/screenshots-tutorials docs/assets/screenshots --max-kb 150 --only $(DOCS_TUTORIAL_SHOTS)

DOCS_EXPLORER_SHOTS := explorer-chooser,explorer-about-card,explorer-data-overview-guided,explorer-data-stationarity,explorer-data-stl,explorer-model-summary,explorer-model-params,explorer-model-residuals,explorer-model-irf,explorer-model-governance,explorer-model-compare,explorer-optimize

.PHONY: docs-screenshots-explorers
docs-screenshots-explorers: ## Recapture the v0.5 explorer, About-card, chooser and optimisation screenshots from mock mode (Playwright)
	MAPI_EXPLORER_SCREENSHOTS=1 MAPI_SMOKE_WEB_PORT=$${MAPI_SMOKE_WEB_PORT:-15173} MAPI_SMOKE_PLOTS_PORT=$${MAPI_SMOKE_PLOTS_PORT:-15174} \
		$(PNPM) --filter @mapi/web exec playwright test e2e/explorer-screenshots.spec.ts
	$(UV) run python scripts/optimise_screenshots.py .local/screenshots-explorers docs/assets/screenshots --max-kb 150 --only $(DOCS_EXPLORER_SHOTS)

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
