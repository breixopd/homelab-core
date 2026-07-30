#!/usr/bin/env bash
# Local CI for homelab-toolkit — parity with .github/workflows/ci.yml + optional fleet gates.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${LOCAL_CI_PYTHON:-3.12}"
HOMELAB_ROOT="${HOMELAB_ROOT:-$REPO_ROOT}"
VENV="${LOCAL_CI_VENV:-$REPO_ROOT/.venv}"
CI_LOG_DIR="$HOMELAB_ROOT/.homelab-state/ci"
RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
LOG_FILE="$CI_LOG_DIR/run-${RUN_ID}.log"

OFFLINE_ONLY=0
E2E_ONLY=0
WITH_FLEET=0
SKIP_OFFLINE=0
KEEP_GOING=0
SKIP_DOCKER=0
ALL_PYTHONS=0
QUICK=0
WITH_COVERAGE=0
CURSOR_SAFE=0
SKIP_GITLEAKS=0

usage() {
  cat <<'EOF'
Usage: scripts/local-ci.sh [options]

Offline gates (default): ruff, mypy, framework/service pytest, gitleaks, tofu, docker, integration, custom images.

Options:
  --root PATH          HOMELAB_ROOT (default: repo root)
  --venv PATH          Python venv (default: .venv)
  --offline-only       Skip fleet phases
  --e2e-only           pytest tests/e2e only
  --with-fleet         Run homelab-toolkit deploy verify --qa
  --skip-offline       Fleet only
  --skip-docker        Skip docker build jobs
  --all-pythons        Run unit tests on 3.11, 3.12, 3.13, 3.14 (requires system interpreters)
  --quick              Lint + mypy + pytest without coverage (Cursor-safe, default offline)
  --cursor-safe        Alias: --quick --skip-docker --offline-only
  --with-coverage      Enforce 70% toolkit.core.* coverage (heavy — run scripts/coverage-chunk.sh outside IDE)
  --keep-going         Continue after first failure
  -h, --help           Show this help

Environment:
  SOPS_AGE_KEY_FILE    Required for --with-fleet on production root
  LOCAL_CI_SKIP_DOCKER=1   Same as --skip-docker
  HOMELAB_LOW_RESOURCE=1   Lower parallel verify probes; prefer --skip-docker
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) HOMELAB_ROOT="$(cd "$2" && pwd)"; shift 2 ;;
    --venv) VENV="$2"; shift 2 ;;
    --offline-only) OFFLINE_ONLY=1; shift ;;
    --e2e-only) E2E_ONLY=1; shift ;;
    --with-fleet) WITH_FLEET=1; shift ;;
    --skip-offline) SKIP_OFFLINE=1; shift ;;
    --skip-docker) SKIP_DOCKER=1; shift ;;
    --all-pythons) ALL_PYTHONS=1; shift ;;
    --quick) QUICK=1; shift ;;
    --cursor-safe) CURSOR_SAFE=1; QUICK=1; SKIP_DOCKER=1; SKIP_GITLEAKS=1; OFFLINE_ONLY=1; shift ;;
    --with-coverage) WITH_COVERAGE=1; shift ;;
    --keep-going) KEEP_GOING=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

[[ "${LOCAL_CI_SKIP_DOCKER:-0}" == "1" ]] && SKIP_DOCKER=1

mkdir -p "$CI_LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

export HOMELAB_ROOT
export CI=1
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}"

FAILED=0
RESULTS=()

run_phase() {
  local name="$1"
  shift
  echo ""
  echo "========== $name =========="
  if "$@"; then
    RESULTS+=("$name:ok")
    echo ">>> $name OK"
  else
    RESULTS+=("$name:FAIL")
    echo ">>> $name FAILED" >&2
    FAILED=1
    if [[ "$KEEP_GOING" -eq 0 ]]; then
      return 1
    fi
  fi
}

activate_venv() {
  command -v uv >/dev/null 2>&1 || { echo "uv is required: https://docs.astral.sh/uv/" >&2; return 1; }
  UV_PROJECT_ENVIRONMENT="$VENV" uv sync --locked --extra test --python "$PYTHON" || return 1
  # shellcheck disable=SC1090
  source "$VENV/bin/activate"
}

phase_lint() {
  ruff check toolkit/ tests/ scripts/ || return 1
  ruff format --check toolkit/ tests/ scripts/
}

phase_mypy() {
  mypy --ignore-missing-imports toolkit/core toolkit/cli toolkit/webui toolkit/controller
}

phase_package() (
  local out
  out="$(mktemp -d)"
  trap 'rm -rf "$out"' EXIT
  uv build --wheel --out-dir "$out" || return 1
  python scripts/check-wheel-contents.py "$out"/*.whl
)

phase_ansible_lint() {
  ANSIBLE_CONFIG="$REPO_ROOT/automation/ansible/ansible.cfg" \
    ansible-lint --project-dir automation/ansible automation/ansible
}

phase_pytest_unit() {
  local py="$1"
  if [[ "$py" != "$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" ]]; then
    UV_PROJECT_ENVIRONMENT="${VENV}-${py}" uv sync --locked --extra test --python "$py" || return 1
    # shellcheck disable=SC1091
    source "${VENV}-${py}/bin/activate" || return 1
  fi
  local -a cov_args=()
  if [[ "$WITH_COVERAGE" -eq 1 ]]; then
    cov_args+=(
      --cov=toolkit.core.config
      --cov=toolkit.core.secrets
      --cov=toolkit.core.generate
      --cov=toolkit.core.deploy
      --cov=toolkit.core.ops
      --cov=toolkit.core.compose
      --cov=toolkit.core.infra
      --cov-report=xml
      --cov-report=term
      --cov-fail-under=70
    )
  fi
  # Low priority so IDE/agent sessions stay responsive.
  nice -n 10 ionice -c3 pytest tests/framework/ \
    "${cov_args[@]}" \
    -q --tb=short --timeout=60 || return 1
  # Service fixtures are intentionally scoped to tests/services. Pytest no
  # longer permits loading that plugin from a combined repository-root run.
  nice -n 10 ionice -c3 pytest tests/services/ -q --tb=short --timeout=120
}

phase_e2e() {
  UV_PROJECT_ENVIRONMENT="$VENV" uv sync --locked --all-extras --python "$PYTHON" || return 1
  nice -n 10 ionice -c3 pytest tests/e2e/ -q --timeout=120 -m e2e
}

phase_gitleaks() {
  if command -v gitleaks >/dev/null 2>&1; then
    gitleaks git --log-opts="--all" --verbose --redact .
  else
    echo "gitleaks is required: https://github.com/gitleaks/gitleaks" >&2
    return 1
  fi
}

phase_tofu() {
  command -v tofu >/dev/null 2>&1 || { echo "opentofu (tofu) not installed"; return 1; }
  homelab-toolkit --root "$REPO_ROOT" generate || return 1
  (cd infrastructure && tofu init -backend=false && tofu validate)
}

phase_docker_toolkit() {
  docker build -t homelab-toolkit:local-ci -f toolkit/Dockerfile . || return 1
  docker run --rm homelab-toolkit:local-ci --help
}

phase_integration() (
  local it_root
  it_root="$(mktemp -d)"
  trap 'rm -rf "$it_root"' EXIT
  python -m toolkit.cli --root "$it_root" config init || return 1
  python -m toolkit.cli --root "$it_root" secrets generate || return 1
  python -m toolkit.cli --root "$it_root" generate || return 1
  python -m toolkit.cli --root "$it_root" ops
)

phase_custom_images() {
  local -a images=()
  local image_output name
  image_output="$(python -m toolkit.cli --root "$REPO_ROOT" images list --ci --names-only)" || return 1
  if [[ -n "$image_output" ]]; then
    mapfile -t images <<<"$image_output"
  fi
  for name in "${images[@]}"; do
    python -m toolkit.cli --root "$REPO_ROOT" images build --image "$name" --registry local --tag ci || return 1
    python -m toolkit.cli --root "$REPO_ROOT" images test --image "$name" --registry local --tag ci || return 1
    python -m toolkit.cli --root "$REPO_ROOT" images audit --image "$name" || return 1
  done
}

phase_fleet_qa() {
  [[ -f "$HOMELAB_ROOT/config.yaml" ]] || { echo "missing $HOMELAB_ROOT/config.yaml"; return 1; }
  [[ -f "$HOMELAB_ROOT/secrets.enc.yaml" ]] || { echo "missing secrets.enc.yaml"; return 1; }
  if [[ -n "${SOPS_AGE_KEY_FILE:-}" ]] && [[ -f "${SOPS_AGE_KEY_FILE}" ]]; then
    sops -d "$HOMELAB_ROOT/secrets.enc.yaml" >/dev/null || return 1
  else
    echo "warn: SOPS_AGE_KEY_FILE not set — deploy verify may fail to load secrets"
  fi
  homelab-toolkit --root "$HOMELAB_ROOT" deploy verify --qa
}

write_summary() {
  local summary="$CI_LOG_DIR/last-run.json"
  {
    echo "{"
    echo "  \"run_id\": \"$RUN_ID\","
    echo "  \"homelab_root\": \"$HOMELAB_ROOT\","
    echo "  \"log_file\": \"$LOG_FILE\","
    echo "  \"failed\": $FAILED,"
    echo "  \"results\": ["
    local first=1 r
    for r in "${RESULTS[@]}"; do
      [[ $first -eq 1 ]] || echo ","
      first=0
      printf '    "%s"' "$r"
    done
    echo ""
    echo "  ]"
    echo "}"
  } >"$summary"
  echo "Summary: $summary"
}

# --- main ---

# Default offline CI is Cursor-safe: no coverage, no docker builds on workstation.
if [[ "$OFFLINE_ONLY" -eq 1 && "$WITH_COVERAGE" -eq 0 && "$QUICK" -eq 0 && "$E2E_ONLY" -eq 0 ]]; then
  QUICK=1
fi
if [[ "$OFFLINE_ONLY" -eq 1 && "$SKIP_DOCKER" -eq 0 && "$E2E_ONLY" -eq 0 ]]; then
  if [[ "${HOMELAB_LOW_RESOURCE:-}" == "1" ]] || [[ -n "${CURSOR_AGENT:-}" ]] || [[ -n "${CURSOR_TRACE_ID:-}" ]]; then
    SKIP_DOCKER=1
  fi
fi

if [[ "$E2E_ONLY" -eq 1 ]]; then
  activate_venv
  run_phase "e2e" phase_e2e || true
  write_summary
  exit "$FAILED"
fi

if [[ "$SKIP_OFFLINE" -eq 0 ]]; then
  activate_venv

  run_phase "lint" phase_lint || true
  [[ "$FAILED" -eq 0 || "$KEEP_GOING" -eq 1 ]] || { write_summary; exit 1; }

  run_phase "mypy" phase_mypy || true
  [[ "$FAILED" -eq 0 || "$KEEP_GOING" -eq 1 ]] || { write_summary; exit 1; }

  run_phase "package" phase_package || true
  [[ "$FAILED" -eq 0 || "$KEEP_GOING" -eq 1 ]] || { write_summary; exit 1; }

  run_phase "ansible-lint" phase_ansible_lint || true
  [[ "$FAILED" -eq 0 || "$KEEP_GOING" -eq 1 ]] || { write_summary; exit 1; }

  if [[ "$ALL_PYTHONS" -eq 1 ]]; then
    for py in 3.11 3.12 3.13 3.14; do
      run_phase "pytest-unit-$py" phase_pytest_unit "$py" || true
      [[ "$FAILED" -eq 0 || "$KEEP_GOING" -eq 1 ]] || break
    done
  else
    run_phase "pytest-unit" phase_pytest_unit "$PYTHON" || true
  fi
  [[ "$FAILED" -eq 0 || "$KEEP_GOING" -eq 1 ]] || { write_summary; exit 1; }

  if [[ "$QUICK" -eq 0 ]]; then
    run_phase "e2e" phase_e2e || true
    [[ "$FAILED" -eq 0 || "$KEEP_GOING" -eq 1 ]] || { write_summary; exit 1; }
  fi

  if [[ "$SKIP_GITLEAKS" -eq 0 ]]; then
    run_phase "gitleaks" phase_gitleaks || true
    [[ "$FAILED" -eq 0 || "$KEEP_GOING" -eq 1 ]] || { write_summary; exit 1; }
  fi

  run_phase "tofu-validate" phase_tofu || true
  [[ "$FAILED" -eq 0 || "$KEEP_GOING" -eq 1 ]] || { write_summary; exit 1; }

  if [[ "$SKIP_DOCKER" -eq 0 ]]; then
    run_phase "docker-toolkit" phase_docker_toolkit || true
    [[ "$FAILED" -eq 0 || "$KEEP_GOING" -eq 1 ]] || { write_summary; exit 1; }
    run_phase "integration" phase_integration || true
    [[ "$FAILED" -eq 0 || "$KEEP_GOING" -eq 1 ]] || { write_summary; exit 1; }
    run_phase "custom-images" phase_custom_images || true
  fi
fi

if [[ "$OFFLINE_ONLY" -eq 1 ]]; then
  write_summary
  exit "$FAILED"
fi

if [[ "$WITH_FLEET" -eq 1 ]] || [[ "$SKIP_OFFLINE" -eq 1 ]]; then
  activate_venv
  run_phase "fleet-qa" phase_fleet_qa || true
fi

write_summary
exit "$FAILED"
