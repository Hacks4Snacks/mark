# Mark — local QA & development helpers.
#
# Quick start:
#   make install    # create .venv and install dev dependencies
#   make qa         # lint + test + build (the local pre-PR gate)
#   make run        # start the app at http://127.0.0.1:8765
#
# Run `make` (or `make help`) to list every target.

PYTHON ?= python3
VENV   := .venv
BIN    := $(VENV)/bin
PY     := $(BIN)/python
PIP    := $(PY) -m pip
STAMP  := $(VENV)/.install-stamp

# Lint/test the same paths CI does.
LINT_PATHS := mark tests

# Knobs for the docker/run targets (override on the command line, e.g.
# `make run PORT=9000`).
IMAGE ?= mark:dev
PORT  ?= 8765

.DEFAULT_GOAL := help


# (Re)install whenever pyproject.toml changes. The stamp lives inside the venv
# so a `make distclean` resets everything.
$(STAMP): pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"
	@touch $(STAMP)

.PHONY: install
install: $(STAMP) ## Create .venv and install the package + dev extras

.PHONY: install-all
install-all: $(STAMP) ## Also install optional extras (semantic, pdf, mcp)
	$(PIP) install -e ".[all,dev]"


.PHONY: lint
lint: $(STAMP) ## Lint with ruff (matches CI)
	$(PY) -m ruff check $(LINT_PATHS)

.PHONY: format
format: $(STAMP) ## Auto-format and apply safe lint fixes
	$(PY) -m ruff format $(LINT_PATHS)
	$(PY) -m ruff check --fix $(LINT_PATHS)

.PHONY: format-check
format-check: $(STAMP) ## Check formatting without modifying files
	$(PY) -m ruff format --check $(LINT_PATHS)

.PHONY: test
test: $(STAMP) ## Run the test suite
	$(PY) -m pytest

.PHONY: build
build: $(STAMP) ## Build sdist + wheel and verify metadata
	$(PIP) install --quiet --upgrade build twine
	$(PY) -m build
	$(PY) -m twine check --strict dist/*

.PHONY: qa
qa: lint test build ## Full local QA gate: lint + test + build

.PHONY: ci
ci: qa docker-smoke ## Everything CI runs, including the container smoke test


.PHONY: run
run: $(STAMP) ## Run the app locally (default port 8765)
	MARK_PORT=$(PORT) $(PY) -m mark

.PHONY: mcp-smoke
mcp-smoke: $(STAMP) ## MCP stdio smoke test (needs the mcp extra + indexed data)
	$(PIP) install --quiet -e ".[mcp]"
	$(PY) scripts/mcp_smoke.py


.PHONY: docker-build
docker-build: ## Build the container image (default tag mark:dev)
	docker build -t $(IMAGE) .

.PHONY: docker-smoke
docker-smoke: docker-build ## Build, run, and probe /api/status, then clean up
	@docker rm -f mark-smoke >/dev/null 2>&1 || true
	docker run -d --name mark-smoke -p $(PORT):8765 $(IMAGE)
	@ok=0; for _ in $$(seq 1 30); do \
	  if curl -fsS http://127.0.0.1:$(PORT)/api/status >/dev/null 2>&1; then ok=1; break; fi; \
	  sleep 2; \
	done; \
	docker logs mark-smoke || true; \
	docker rm -f mark-smoke >/dev/null 2>&1 || true; \
	if [ "$$ok" -ne 1 ]; then echo "::: smoke test FAILED"; exit 1; fi; \
	echo "::: smoke test OK"

.PHONY: compose-up
compose-up: ## Start the full stack via docker compose (build + detach)
	docker compose up --build -d

.PHONY: compose-down
compose-down: ## Stop the docker compose stack
	docker compose down


.PHONY: clean
clean: ## Remove build artifacts and caches
	rm -rf dist *.egg-info .pytest_cache .ruff_cache .coverage htmlcov
	find $(LINT_PATHS) -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

.PHONY: distclean
distclean: clean ## Also remove the virtualenv
	rm -rf $(VENV)

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "; printf "Mark — make targets:\n\n"} \
	  /^[a-zA-Z0-9_.-]+:.*## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' \
	  $(MAKEFILE_LIST)
