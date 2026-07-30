#!/usr/bin/env bash
# Low-RAM pytest wrapper for dev workstations / Cursor agent sessions.
# Never runs full-suite coverage unless explicitly allowed.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${HOMELAB_VENV:-$REPO_ROOT/.venv}"
PYTEST="${VENV}/bin/pytest"

ALLOW_FULL=0
ALLOW_COV=0
EXTRA=()

usage() {
  cat <<'EOF'
Usage: scripts/pytest-safe.sh [options] [pytest args...]

Defaults: nice/ionice, no coverage, blocks full tests/framework/ unless --allow-full.

Options:
  --allow-full    Permit running entire tests/framework/ tree
  --allow-cov     Permit --cov / coverage instrumentation (heavy)
  -h, --help      Show help

Examples:
  scripts/pytest-safe.sh tests/framework/toolkit/test_hooks.py -q
  scripts/pytest-safe.sh --allow-full --allow-cov   # outside IDE only
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-full) ALLOW_FULL=1; shift ;;
    --allow-cov) ALLOW_COV=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) EXTRA+=("$1"); shift ;;
  esac
done

if [[ ! -x "$PYTEST" ]]; then
  echo "Missing venv pytest at $PYTEST — run: uv sync --locked --extra test" >&2
  exit 1
fi

# Block accidental full-suite / coverage from agents.
for arg in "${EXTRA[@]}"; do
  case "$arg" in
    tests/framework|tests/framework/|tests/framework/*)
      if [[ "$ALLOW_FULL" -eq 0 && ( "$arg" == "tests/framework" || "$arg" == "tests/framework/" ) ]]; then
        echo "Refusing full framework suite (RAM). Pass paths or --allow-full." >&2
        exit 2
      fi
      ;;
    --cov*|--cov)
      if [[ "$ALLOW_COV" -eq 0 ]]; then
        echo "Refusing coverage (RAM). Use --allow-cov outside IDE or scripts/coverage-chunk.sh." >&2
        exit 2
      fi
      ;;
  esac
done

export HOMELAB_LOW_RESOURCE="${HOMELAB_LOW_RESOURCE:-1}"
cd "$REPO_ROOT"
exec nice -n 15 ionice -c3 "$PYTEST" \
  -q --tb=short --timeout=60 \
  "${EXTRA[@]}"
