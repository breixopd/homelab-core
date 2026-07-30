#!/usr/bin/env bats
# Smoke tests for scripts/install.sh (CI source-validation job).

setup() {
  export REPO_ROOT
  REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
  export HOMELAB_SMOKE_ROOT="${BATS_TEST_TMPDIR}/smoke"
  cd "$REPO_ROOT" || exit 1
}

@test "install.sh smoke-test creates merged .env and Caddyfile" {
  run bash scripts/install.sh --preset all --smoke-test --yes
  if [ "$status" -ne 0 ]; then
    printf '%s\n' "$output" >&3
  fi
  [ "$status" -eq 0 ]
  [ -f "${HOMELAB_SMOKE_ROOT}/.env" ]
  [ -f "${HOMELAB_SMOKE_ROOT}/generated/Caddyfile" ]
}

@test "install.sh rejects unknown flags" {
  run bash scripts/install.sh --not-a-real-flag
  [ "$status" -ne 0 ]
}
