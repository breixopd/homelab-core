# Local CI wrappers — see scripts/local-ci.sh
REPO_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
HOMELAB_ROOT ?= $(REPO_ROOT)
VENV ?= $(REPO_ROOT)/.venv
PYTHON ?= python3.12
UV ?= uv

.PHONY: help ci ci-offline ci-cov ci-fleet ci-e2e ci-cursor verify-phase2 lint format format-check mypy ansible-lint test test-unit test-safe test-cov coverage-chunk test-e2e \
	        package-check tofu-validate docker-toolkit integration gitleaks venv

help:
	@echo "Targets:"
	@echo "  make ci              Quick offline gates (lint, test — no coverage)"
	@echo "  make ci-cov          Offline gates with 70% coverage (run outside IDE)"
	@echo "  make ci-fleet        Offline + deploy verify --qa"
	@echo "  make ci-offline      Same as ci"
	@echo "  make ci-cursor       Cursor-safe: lint + mypy + unit tests, no docker/coverage"
	@echo "  make test-safe PATH=...  Low-RAM pytest wrapper (see scripts/pytest-safe.sh)"
	@echo "  make coverage-chunk  70% coverage in RAM-safe chunks (run outside IDE)"
	@echo "  make verify-phase2   Live fleet Phase 0–2 gates (scripts/verify-phase2.sh)"
	@echo "  make lint / mypy / ansible-lint / test-unit / test-e2e / package-check"

venv:
	@command -v "$(UV)" >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }
	@UV_PROJECT_ENVIRONMENT="$(VENV)" $(UV) sync --locked --extra test --python "$(PYTHON)"

ci ci-offline:
	@HOMELAB_LOW_RESOURCE=1 HOMELAB_ROOT="$(HOMELAB_ROOT)" ./scripts/local-ci.sh --offline-only --quick

ci-cursor:
	@HOMELAB_LOW_RESOURCE=1 HOMELAB_ROOT="$(HOMELAB_ROOT)" ./scripts/local-ci.sh --cursor-safe

verify-phase2:
	@HOMELAB_ROOT="$(HOMELAB_ROOT)" ./scripts/verify-phase2.sh

ci-cov:
	@./scripts/coverage-chunk.sh

ci-fleet:
	@HOMELAB_ROOT="$(HOMELAB_ROOT)" ./scripts/local-ci.sh --with-fleet

ci-e2e:
	@./scripts/local-ci.sh --e2e-only

lint: venv
	@$(VENV)/bin/ruff check toolkit/ tests/ scripts/

format-check: venv
	@$(VENV)/bin/ruff format --check toolkit/ tests/ scripts/

format: venv
	@$(VENV)/bin/ruff format toolkit/ tests/ scripts/

mypy: venv
	@$(VENV)/bin/mypy --ignore-missing-imports toolkit/core toolkit/cli toolkit/webui toolkit/controller

ansible-lint: venv
	@ANSIBLE_CONFIG="$(REPO_ROOT)/automation/ansible/ansible.cfg" \
	  $(VENV)/bin/ansible-lint --project-dir automation/ansible automation/ansible

test test-unit: venv
	@HOMELAB_LOW_RESOURCE=1 nice -n 15 ionice -c3 $(VENV)/bin/pytest tests/framework/ -q --tb=short --timeout=60
	@HOMELAB_LOW_RESOURCE=1 nice -n 15 ionice -c3 $(VENV)/bin/pytest tests/services/ -q --tb=short --timeout=120

test-safe: venv
	@HOMELAB_LOW_RESOURCE=1 ./scripts/pytest-safe.sh $(PATH_ARGS)

coverage-chunk: venv
	@./scripts/coverage-chunk.sh

test-cov: venv
	@nice -n 10 ionice -c3 $(VENV)/bin/pytest tests/framework/ \
	  --cov=toolkit.core.config --cov=toolkit.core.secrets --cov=toolkit.core.generate \
	  --cov=toolkit.core.deploy --cov=toolkit.core.ops --cov=toolkit.core.compose \
	  --cov=toolkit.core.infra --cov-report=term --cov-fail-under=70 -q --tb=short --timeout=60

test-e2e: venv
	@UV_PROJECT_ENVIRONMENT="$(VENV)" $(UV) sync --locked --all-extras
	@$(VENV)/bin/pytest tests/e2e/ -v --timeout=120 -m e2e

package-check: venv
	@out=$$(mktemp -d); trap 'rm -rf "$$out"' EXIT; \
	$(UV) build --wheel --out-dir "$$out"; \
	$(VENV)/bin/python scripts/check-wheel-contents.py "$$out"/*.whl

tofu-validate: venv
	@PYTHONPATH="$(REPO_ROOT)" $(VENV)/bin/homelab-toolkit --root "$(REPO_ROOT)" generate
	@cd infrastructure && tofu init -backend=false && tofu validate

docker-toolkit:
	@docker build -t homelab-toolkit:local-ci -f toolkit/Dockerfile toolkit/
	@docker run --rm homelab-toolkit:local-ci --help

integration: venv
	@IT_ROOT=$$(mktemp -d); trap 'rm -rf $$IT_ROOT' EXIT; \
	$(VENV)/bin/python -m toolkit.cli --root $$IT_ROOT config init; \
	$(VENV)/bin/python -m toolkit.cli --root $$IT_ROOT secrets generate; \
	$(VENV)/bin/python -m toolkit.cli --root $$IT_ROOT generate; \
	$(VENV)/bin/python -m toolkit.cli --root $$IT_ROOT ops

gitleaks:
	@gitleaks git --log-opts="--all" --verbose --redact .
