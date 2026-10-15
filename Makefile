# modeling-api — developer entry points.  Run `make help` for the list.
# Every target is safe to run from a fresh clone after `make install`.

SHELL := /bin/bash
UV ?= uv
PNPM ?= pnpm
PY_DIRS := packages plugins conformance
CORPUS_SIZE ?= tiny

.DEFAULT_GOAL := help

## ---------------------------------------------------------------- setup
.PHONY: install
install: ## Install all Python workspace packages + dev tools (and JS deps if apps/ exist)
	$(UV) sync --all-packages
	@if [ -f pnpm-workspace.yaml ] && [ -d apps ]; then $(PNPM) install; fi

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

.PHONY: typecheck
typecheck: ## mypy over every type-checked package (spec, sdk, steps, pymc, sklearn, plots, plugins, corpus)
	$(UV) run mypy packages/spec/src packages/sdk/src packages/steps-core/src packages/sdk-pymc/src \
		packages/sdk-sklearn/src packages/plots-core/src plugins/mmm/src plugins/ts/src \
		plugins/tabular/src conformance/src
	$(UV) run mypy --strict packages/server/src

## ---------------------------------------------------------------- tests
.PHONY: test
test: ## Run the default local test suite (no docker/cloud/e2e)
	$(UV) run pytest -q

.PHONY: test-fast
test-fast: ## Unit tests only, skipping slow MCMC tests
	$(UV) run pytest -q -m "not slow and not docker and not cloud and not e2e"

.PHONY: conformance
conformance: ## Run the conformance corpus at tiny size (FIXTURE=<id> picks one; CORPUS_SIZE overrides)
	$(UV) run python -m mapi_conformance run --size $(CORPUS_SIZE) $(if $(FIXTURE),--fixture $(FIXTURE))

.PHONY: conformance-full
conformance-full: ## Run the conformance corpus at full size (nightly-scale data and fits; slow)
	$(UV) run python -m mapi_conformance run --size full $(if $(FIXTURE),--fixture $(FIXTURE))

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

## ---------------------------------------------------------------- housekeeping
.PHONY: clean
clean: ## Remove caches and build output (keeps .venv and .local)
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov site dist build
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage: make <target>\n"} \
	  /^## -+ / {printf "\n\033[1m%s\033[0m\n", substr($$0, index($$0,$$3))} \
	  /^[a-zA-Z0-9_.-]+:.*##/ {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
