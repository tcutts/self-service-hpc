# ---------------------------------------------------------------------------
# Self-Service HPC Platform — Makefile
#
# Targets:
#   deploy    — full deployment (foundation + project stacks)
#   teardown  — destroy all clusters and projects, keep foundation
#   purge     — teardown + destroy foundation stack
#   build     — compile CDK TypeScript
#   test      — run CDK (jest) and Python (pytest) tests
#   synth     — synthesise CloudFormation templates
# ---------------------------------------------------------------------------

SHELL       := /bin/bash
.DEFAULT_GOAL := help

AWS_PROFILE := thecutts
VENV_DIR    := .venv
PYTHON      := $(VENV_DIR)/bin/python
PIP         := $(VENV_DIR)/bin/pip
PYTEST      := $(VENV_DIR)/bin/pytest

# ---------------------------------------------------------------------------
# Helper targets
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.PHONY: venv
venv: .venv/.installed ## Create Python virtual environment and install dependencies

.venv/.installed: requirements.txt
	@if [ ! -d "$(VENV_DIR)" ]; then python3 -m venv $(VENV_DIR); fi
	$(PIP) install --quiet -r requirements.txt
	@touch .venv/.installed

.PHONY: node_modules
node_modules: node_modules/.installed ## Install Node.js dependencies

node_modules/.installed: package.json package-lock.json
	npm ci
	@touch node_modules/.installed

.PHONY: build
build: node_modules/.installed ## Compile CDK TypeScript
	npm run build

.PHONY: synth
synth: build .venv/.installed ## Synthesise CloudFormation templates
	npx cdk synth --profile $(AWS_PROFILE)

.PHONY: test
test: build .venv/.installed ## Run CDK (jest) and Python (pytest) tests
	npm test
	$(PYTEST) test/lambda/ -v

# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------

.PHONY: deploy
deploy: build .venv/.installed ## Deploy foundation and all project stacks
	npx cdk deploy HpcFoundationStack --require-approval never --profile $(AWS_PROFILE)
	@echo "--- Deploying project stacks (HpcProject-*) ---"
	@for stack in $$(npx cdk list --profile $(AWS_PROFILE) 2>/dev/null | grep '^HpcProject-'); do \
		echo "Deploying $$stack ..."; \
		npx cdk deploy "$$stack" --require-approval never --profile $(AWS_PROFILE); \
	done
	@echo "--- Deployment complete ---"

# ---------------------------------------------------------------------------
# Teardown (destroy workloads, keep foundation)
# ---------------------------------------------------------------------------

.PHONY: teardown
teardown: .venv/.installed ## Destroy all clusters and projects, retain foundation stack
	$(PYTHON) scripts/teardown_workloads.py --profile $(AWS_PROFILE)

# ---------------------------------------------------------------------------
# Purge (teardown + destroy foundation)
# ---------------------------------------------------------------------------

.PHONY: purge
purge: teardown ## Full purge: teardown workloads then destroy foundation stack
	npx cdk destroy HpcFoundationStack --force --profile $(AWS_PROFILE)
	@echo "--- Purge complete ---"
