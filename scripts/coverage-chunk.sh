#!/usr/bin/env bash
# Single-pass coverage for toolkit.core.* (run outside IDE with Docker stacks stopped).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${HOMELAB_VENV:-$REPO_ROOT/.venv}"
PYTEST="${VENV}/bin/pytest"
FAIL_UNDER="${COVERAGE_FAIL_UNDER:-70}"

cd "$REPO_ROOT"
echo "Running single-pass coverage (fail-under=${FAIL_UNDER}%) — low priority"
nice -n 19 ionice -c3 "$PYTEST" tests/framework/ \
  --cov=toolkit.core.config \
  --cov=toolkit.core.secrets \
  --cov=toolkit.core.generate \
  --cov=toolkit.core.deploy \
  --cov=toolkit.core.ops \
  --cov=toolkit.core.compose \
  --cov=toolkit.core.infra \
  --cov-report=term-missing:skip-covered \
  --cov-fail-under="$FAIL_UNDER" \
  -q --tb=line --timeout=60
