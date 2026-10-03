# Makefile for learning-ecs.
#
# Every target here is a *shortcut* for commands documented step by step
# elsewhere (REQUIREMENTS.md, CONTRIBUTING.md, docs/TESTING.md, and every
# module's README). Learn the long form first - `uv run cdk synth FargateServiceStack`,
# `docker compose up -d floci`, ... - then use these once you know what
# they run. `make` (or `make help`) lists every target.

SHELL := /bin/bash

.DEFAULT_GOAL := help

# A module's stack id (see docs/LEARNING-PATH.md, or `uv run cdk list`).
# cdk-synth: empty = every stack. cdk-deploy: required ("all" = every stack).
STACK ?=
# SKIP_COVERAGE_CHECK=1 makes `make coverage` report without failing below 80%.
SKIP_COVERAGE_CHECK ?=
# Variables loaded before every cdk-* command: your .env if it exists,
# otherwise .env.example (which already points at floci).
ENV_FILE ?= $(if $(wildcard .env),.env,.env.example)
# Extra regions for floci-prune/cdk-resources (module 18 deploys to several).
REGIONS ?=

COMPOSE := docker compose
FLOCI_CONTAINER := learning-ecs-floci
FLOCI_IMAGE := floci/floci:latest

LOAD_ENV := set -a; source $(ENV_FILE); set +a;

.PHONY: help check typecheck lint test coverage cdk-synth cdk-diff cdk-deploy cdk-destroy cdk-resources \
	floci-start floci-stop floci-status floci-destroy floci-prune

help: ## Show this list of targets
	@echo "Targets:"
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*## "}; {printf "  %-14s %s\n", $$1, $$2}'
	@echo ""
	@echo "Examples:"
	@echo "  make cdk-synth STACK=FargateServiceStack   make cdk-deploy STACK=AlbStack"
	@echo "  make cdk-resources STACK=AlbStack          make cdk-destroy STACK=AlbStack"

check: ## Check your OS + every required/recommended/optional tool (see REQUIREMENTS.md section 3)
	@bash scripts/check-deps.sh

# Module directories start with a digit (01_network, ...), which is not a
# valid Python package name, so each stack.py is checked on its own.
typecheck: ## Type-check shared/, app.py, scripts/ and every module's stack.py with mypy
	@uv run mypy shared app.py
	@uv run mypy scripts/floci_prune.py scripts/resource_commands.py
	@set -e; for dir in modules/*/; do \
		[ -f "$${dir}stack.py" ] || continue; \
		echo "mypy $${dir}stack.py"; \
		MYPYPATH="$$dir" uv run mypy --explicit-package-bases --no-error-summary "$${dir}stack.py"; \
	done

lint: ## Lint every Python file with ruff
	uv run ruff check .

test: ## Run every unit test (no Docker, floci or AWS credentials needed)
	uv run pytest -q

coverage: ## Run every test with a coverage report (fails below 80%; SKIP_COVERAGE_CHECK=1 to only report)
	@if [ -n "$(SKIP_COVERAGE_CHECK)" ]; then \
		echo "SKIP_COVERAGE_CHECK is set: reporting coverage without enforcing the 80% minimum."; \
	fi
	uv run pytest tests --cov --cov-report=term-missing --cov-report=html \
		$(if $(SKIP_COVERAGE_CHECK),--cov-fail-under=0)

# --- floci (the local AWS emulator) -------------------------------------------

floci-status: ## Show whether floci is running, and its URLs
	@state="$$(docker inspect -f '{{.State.Status}}{{if and .State.Running .State.Health}} ({{.State.Health.Status}}){{end}}' $(FLOCI_CONTAINER) 2>/dev/null || echo 'not created')"; \
	echo "floci: $$state"
	@echo "  API:              http://localhost:4566"
	@echo "  Built-in console: http://localhost:4566/_floci/ui"
	@echo "  ECS task containers: docker ps --filter label=io.floci.service=ecs"

floci-start: ## Start floci and wait until it's healthy
	@if [ "$$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' $(FLOCI_CONTAINER) 2>/dev/null)" = "healthy" ]; then \
		echo "floci is already running and healthy."; \
	else \
		$(COMPOSE) up -d floci && \
		echo -n "Waiting for floci to be healthy" && \
		for i in $$(seq 1 60); do \
			[ "$$(docker inspect -f '{{.State.Health.Status}}' $(FLOCI_CONTAINER) 2>/dev/null)" = "healthy" ] && { echo " - ready."; exit 0; }; \
			echo -n "."; sleep 2; \
		done; \
		echo ""; echo "floci did not become healthy in 120s - see: docker compose logs floci" >&2; exit 1; \
	fi

floci-stop: ## Stop floci, keeping its container and data
	$(COMPOSE) stop

# Containers floci started for deployed resources (ECS tasks, RDS,
# ElastiCache, DocumentDB, the ECR registry, ...) aren't Compose services,
# so `down` would leave them behind - floci labels every one with floci=true.
# ./.floci is owned by root (written by the container), so it is deleted
# from inside a throwaway floci container instead of with a plain `rm`.
floci-destroy: ## Remove floci and ALL deployed local resources (asks first; CONFIRM=yes skips)
	@if [ "$(CONFIRM)" != "yes" ]; then \
		read -r -p "Delete floci's containers and every resource deployed to it (./.floci/data)? Type 'yes': " answer; \
		[ "$$answer" = "yes" ] || { echo "Cancelled."; exit 1; }; \
	fi
	$(COMPOSE) stop
	@ids="$$(docker ps -aq --filter label=floci=true)"; \
	if [ -n "$$ids" ]; then docker rm -f $$ids >/dev/null && echo "Removed $$(echo $$ids | wc -w) container(s) floci had started."; fi
	@vols="$$(docker volume ls -q --filter label=floci=true)"; \
	if [ -n "$$vols" ]; then docker volume rm $$vols >/dev/null && echo "Removed $$(echo $$vols | wc -w) volume(s) floci had created."; fi
	$(COMPOSE) down --remove-orphans
	@if [ -d .floci ]; then \
		docker run --rm --entrypoint bash -v "$(CURDIR):/repo" $(FLOCI_IMAGE) -c 'rm -rf /repo/.floci' && \
		echo "Deleted ./.floci (all local floci data)."; \
	fi

cdk-resources: ## List every resource a deployed stack created, via the AWS CLI (STACK=AlbStack or STACK=all); read-only
	@if [ -z "$(STACK)" ]; then \
		echo "Pick what to list: make cdk-resources STACK=AlbStack, or STACK=all." >&2; exit 1; \
	fi
	@$(MAKE) --no-print-directory floci-start
	$(LOAD_ENV) uv run python scripts/resource_commands.py $(if $(filter-out all,$(STACK)),$(STACK),--all) $(foreach r,$(REGIONS),--region $(r))

# floci's CloudFormation never deletes AWS::EC2::VPC resources on stack
# deletion - see REQUIREMENTS.md section 5.7. Refuses to run against real AWS.
floci-prune: ## List VPCs floci left behind after cdk destroy; APPLY=yes deletes them (floci only)
	$(LOAD_ENV) uv run python scripts/floci_prune.py $(foreach r,$(REGIONS),--region $(r)) $(if $(filter yes,$(APPLY)),--apply)

# --- CDK shortcuts -------------------------------------------------------------

cdk-synth: ## Synthesize one stack (STACK=AlbStack) or every stack
	$(LOAD_ENV) uv run cdk synth $(if $(STACK),$(STACK),--all --quiet)

cdk-diff: ## Show what cdk deploy would change (STACK=AlbStack or every stack); changes nothing
	@$(MAKE) --no-print-directory floci-start
	$(LOAD_ENV) uv run cdk diff $(if $(filter-out all,$(STACK)),$(STACK),--all)

cdk-deploy: ## Deploy to floci: STACK=AlbStack, or STACK=all
	@if [ -z "$(STACK)" ]; then \
		echo "Pick what to deploy: make cdk-deploy STACK=AlbStack (see 'uv run cdk list'), or STACK=all." >&2; exit 1; \
	fi
	@$(LOAD_ENV) if [ -z "$${AWS_ENDPOINT_URL:-}" ]; then \
		echo "AWS_ENDPOINT_URL is not set in $(ENV_FILE) - cdk-deploy only targets floci. To deploy to real AWS, follow the module README's 'Deploy to real AWS' section instead." >&2; exit 1; \
	fi
	@$(MAKE) --no-print-directory floci-start
	$(LOAD_ENV) uv run cdk bootstrap --quiet
	$(LOAD_ENV) uv run cdk deploy $(if $(filter-out all,$(STACK)),$(STACK),--all) --require-approval never --method=direct

cdk-destroy: ## Destroy from floci (STACK=AlbStack or STACK=all), then delete the VPCs floci leaves
	@if [ -z "$(STACK)" ]; then \
		echo "Pick what to destroy: make cdk-destroy STACK=AlbStack, or STACK=all." >&2; exit 1; \
	fi
	@$(LOAD_ENV) if [ -z "$${AWS_ENDPOINT_URL:-}" ]; then \
		echo "AWS_ENDPOINT_URL is not set in $(ENV_FILE) - cdk-destroy only targets floci." >&2; exit 1; \
	fi
	$(LOAD_ENV) uv run cdk destroy $(if $(filter-out all,$(STACK)),$(STACK),--all) --force
	@$(MAKE) --no-print-directory floci-prune APPLY=yes
