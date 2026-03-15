# ─────────────────────────────────────────────────────────────────────────────
# CloudOps AI Agent — Makefile
# Usage: make <target>
# ─────────────────────────────────────────────────────────────────────────────

.DEFAULT_GOAL := help
SHELL         := /bin/bash
PYTHON        := python3
PIP           := pip3
VENV          := .venv
VENV_PYTHON   := $(VENV)/bin/python
VENV_PIP      := $(VENV)/bin/pip
SRC_DIRS      := agents tools app.py
TEST_DIR      := tests
SCRIPTS_DIR   := scripts
ENV           ?= staging                     # override: make deploy ENV=prod
REGION        ?= us-east-1
PACKAGE_FILE  := lambda_package.zip

# Colours for output
GREEN  := \033[0;32m
YELLOW := \033[1;33m
RESET  := \033[0m

# ─────────────────────────────────────────────────────────────────────────────
# Help
# ─────────────────────────────────────────────────────────────────────────────
.PHONY: help
help:  ## Show this help message
	@echo ""
	@echo "  CloudOps AI Agent"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-22s$(RESET) %s\n", $$1, $$2}'
	@echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────────────────────────────────────
.PHONY: venv
venv:  ## Create virtual environment
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install --upgrade pip
	@echo "$(GREEN)Virtual environment created. Run: source $(VENV)/bin/activate$(RESET)"

.PHONY: install
install: venv  ## Install production dependencies
	$(VENV_PIP) install -r requirements.txt
	@echo "$(GREEN)Production dependencies installed.$(RESET)"

.PHONY: install-dev
install-dev: venv  ## Install all dependencies including dev/test
	$(VENV_PIP) install -r requirements-dev.txt
	@echo "$(GREEN)Dev dependencies installed.$(RESET)"

.PHONY: env
env:  ## Copy .env.example → .env (skips if .env exists)
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "$(YELLOW).env created from .env.example — please fill in your values.$(RESET)"; \
	else \
		echo ".env already exists, skipping."; \
	fi

# ─────────────────────────────────────────────────────────────────────────────
# Quality gates
# ─────────────────────────────────────────────────────────────────────────────
.PHONY: lint
lint:  ## Run ruff linter
	$(VENV_PYTHON) -m ruff check $(SRC_DIRS) $(TEST_DIR) $(SCRIPTS_DIR)

.PHONY: format
format:  ## Auto-format with ruff
	$(VENV_PYTHON) -m ruff format $(SRC_DIRS) $(TEST_DIR) $(SCRIPTS_DIR)
	$(VENV_PYTHON) -m ruff check --fix $(SRC_DIRS) $(TEST_DIR) $(SCRIPTS_DIR)

.PHONY: typecheck
typecheck:  ## Run mypy type checker
	$(VENV_PYTHON) -m mypy $(SRC_DIRS)

.PHONY: check
check: lint typecheck  ## Run all quality checks (no tests)

# ─────────────────────────────────────────────────────────────────────────────
# Testing
# ─────────────────────────────────────────────────────────────────────────────
.PHONY: test
test:  ## Run all unit tests
	$(VENV_PYTHON) -m pytest $(TEST_DIR) -m "not integration" -v

.PHONY: test-cov
test-cov:  ## Run tests with coverage report
	$(VENV_PYTHON) -m pytest $(TEST_DIR) -m "not integration" \
		--cov=agents --cov=tools --cov=app \
		--cov-report=term-missing \
		--cov-report=html:htmlcov \
		-v
	@echo "$(GREEN)Coverage report: htmlcov/index.html$(RESET)"

.PHONY: test-integration
test-integration:  ## Run integration tests (requires AWS credentials)
	$(VENV_PYTHON) -m pytest $(TEST_DIR) -m integration -v

.PHONY: test-all
test-all:  ## Run unit + integration tests
	$(VENV_PYTHON) -m pytest $(TEST_DIR) -v

.PHONY: test-file
test-file:  ## Run a single test file: make test-file FILE=tests/test_incident_agent.py
	$(VENV_PYTHON) -m pytest $(FILE) -v

# ─────────────────────────────────────────────────────────────────────────────
# Local invocation
# ─────────────────────────────────────────────────────────────────────────────
.PHONY: run
run:  ## Run the pipeline locally with sample incident
	$(VENV_PYTHON) app.py

.PHONY: invoke
invoke:  ## Invoke with custom description: make invoke DESC="Lambda X has errors"
	$(VENV_PYTHON) $(SCRIPTS_DIR)/invoke_local.py --description "$(DESC)"

# ─────────────────────────────────────────────────────────────────────────────
# Build & package
# ─────────────────────────────────────────────────────────────────────────────
.PHONY: package
package:  ## Build Lambda deployment ZIP
	@echo "$(YELLOW)Building Lambda package...$(RESET)"
	rm -rf lambda_package/ $(PACKAGE_FILE)
	mkdir -p lambda_package
	$(VENV_PIP) install -r requirements.txt --target lambda_package/ --quiet
	cp -r agents tools app.py lambda_package/
	cd lambda_package && zip -r ../$(PACKAGE_FILE) . -x "*.pyc" -x "*/__pycache__/*" -q
	rm -rf lambda_package/
	@echo "$(GREEN)Package ready: $(PACKAGE_FILE) ($$(du -sh $(PACKAGE_FILE) | cut -f1))$(RESET)"

# ─────────────────────────────────────────────────────────────────────────────
# Deploy
# ─────────────────────────────────────────────────────────────────────────────
.PHONY: deploy
deploy: package  ## Deploy to Lambda: make deploy ENV=prod
	@echo "$(YELLOW)Deploying to Lambda [$(ENV)]...$(RESET)"
	bash $(SCRIPTS_DIR)/deploy.sh $(ENV)
	@echo "$(GREEN)Deploy complete.$(RESET)"

.PHONY: deploy-prod
deploy-prod:  ## Deploy directly to production (requires approval)
	@echo "$(YELLOW)⚠️  Deploying to PRODUCTION. Press Ctrl-C to cancel...$(RESET)"
	@sleep 5
	$(MAKE) deploy ENV=prod

# ─────────────────────────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────────────────────────
.PHONY: clean
clean:  ## Remove build artifacts
	rm -rf lambda_package/ $(PACKAGE_FILE) \
		htmlcov/ .coverage .coverage.* coverage.xml \
		.pytest_cache/ .mypy_cache/ .ruff_cache/ \
		**/__pycache__/ **/*.pyc **/*.pyo
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; true
	find . -type f -name "*.pyc"       -delete 2>/dev/null; true
	@echo "$(GREEN)Cleaned.$(RESET)"

.PHONY: clean-venv
clean-venv: clean  ## Remove virtual environment too
	rm -rf $(VENV)
	@echo "$(GREEN)Virtual environment removed.$(RESET)"

# ─────────────────────────────────────────────────────────────────────────────
# CI shortcut (used by GitHub Actions)
# ─────────────────────────────────────────────────────────────────────────────
.PHONY: ci
ci: install-dev lint typecheck test-cov  ## Full CI pipeline (install → lint → typecheck → test)
