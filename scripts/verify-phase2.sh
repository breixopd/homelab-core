#!/usr/bin/env bash
# Phase 0–2 verification on the live stack (controller with SOPS + SSH to guests).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOMELAB_ROOT="${HOMELAB_ROOT:-$REPO_ROOT}"
cd "$HOMELAB_ROOT"

export HOMELAB_ROOT

usage() {
  cat <<'EOF'
Usage: scripts/verify-phase2.sh [options]

Runs the full Phase 0–2 gate sequence against the live fleet.

Options:
  --skip-generate     Skip homelab-toolkit generate
  --skip-sync         Skip machine sync for configured targets
  --with-recover      Print recover drill reminder after gates
  -h, --help          Show help

Requires: SOPS_AGE_KEY_FILE and SSH to Proxmox LXCs.
EOF
}

SKIP_GENERATE=0
SKIP_SYNC=0
WITH_RECOVER=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-generate) SKIP_GENERATE=1; shift ;;
    --skip-sync) SKIP_SYNC=1; shift ;;
    --with-recover) WITH_RECOVER=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "${SOPS_AGE_KEY_FILE:-}" ]]; then
  echo "SOPS_AGE_KEY_FILE is required" >&2
  exit 1
fi

TOOLKIT=(uv run homelab-toolkit --root "$HOMELAB_ROOT")

run_step() {
  echo ""
  echo "=== $* ==="
  "$@"
}

if [[ "$SKIP_GENERATE" -eq 0 ]]; then
  run_step "${TOOLKIT[@]}" generate
fi

if [[ "$SKIP_SYNC" -eq 0 ]]; then
  for vm in infra media apps; do
    run_step "${TOOLKIT[@]}" machines sync "$vm"
  done
fi

for vm in infra media apps; do
  run_step "${TOOLKIT[@]}" deploy hooks --vm "$vm"
done

run_step "${TOOLKIT[@]}" deploy verify --hooks
run_step "${TOOLKIT[@]}" deploy verify --qa

run_step make -C "$REPO_ROOT" ci-cursor

echo ""
echo "Phase 0–2 gates passed."

if [[ "$WITH_RECOVER" -eq 1 ]]; then
  echo ""
  echo "Optional G65 recover drill (manual): scripts/recover-drill.sh --vm infra"
fi
