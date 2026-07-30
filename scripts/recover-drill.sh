#!/usr/bin/env bash
# G65 recover drill: induce a hook failure, recover, re-verify.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOMELAB_ROOT="${HOMELAB_ROOT:-$REPO_ROOT}"

usage() {
  cat <<'EOF'
Usage: scripts/recover-drill.sh [--vm infra|media|apps] [--execute]

Validates deploy recover (G65) on the live stack.

Without --execute: prints the drill steps.

With --execute (infra only, safe target):
  1. Stop ntfy container briefly on the VM
  2. Run deploy hooks (expect failure)
  3. Start ntfy
  4. Run deploy recover
  5. Run deploy verify --hooks

Requires SOPS_AGE_KEY_FILE and SSH to guests.
EOF
}

VM=""
EXECUTE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vm) VM="$2"; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown: $1" >&2; usage; exit 2 ;;
  esac
done

VM="${VM:-infra}"
export HOMELAB_ROOT
TOOLKIT=(uv run homelab-toolkit --root "$HOMELAB_ROOT")

if [[ "$EXECUTE" -eq 0 ]]; then
  cat <<EOF
G65 recover drill (${VM}):

  1. Induce a transient hook failure (example: stop ntfy on ${VM})
  2. ${TOOLKIT[*]} deploy hooks --vm ${VM}   # expect non-zero
  3. Restore the service
  4. ${TOOLKIT[*]} deploy recover --vm ${VM}
  5. ${TOOLKIT[*]} deploy verify --hooks

Re-run with --execute to run the automated ntfy stop/start drill on infra.
EOF
  exit 0
fi

if [[ "$VM" != "infra" ]]; then
  echo "--execute is only supported for --vm infra (safe transient target)" >&2
  exit 2
fi

if [[ -z "${SOPS_AGE_KEY_FILE:-}" ]]; then
  echo "SOPS_AGE_KEY_FILE is required for --execute" >&2
  exit 1
fi

set +e
"${TOOLKIT[@]}" deploy hooks --vm infra
before=$?
set -e
if [[ "$before" -eq 0 ]]; then
  echo "Stopping ntfy to induce hook failure..."
  "${TOOLKIT[@]}" machines exec infra docker stop ntfy
  set +e
  "${TOOLKIT[@]}" deploy hooks --vm infra
  hooks_failed=$?
  set -e
  echo "Starting ntfy..."
  "${TOOLKIT[@]}" machines exec infra docker start ntfy
  if [[ "$hooks_failed" -eq 0 ]]; then
    echo "Hooks did not fail after stopping ntfy — drill inconclusive" >&2
    exit 1
  fi
else
  echo "Hooks already failing before drill — fix stack first" >&2
  exit 1
fi

"${TOOLKIT[@]}" deploy recover --vm infra
"${TOOLKIT[@]}" deploy verify --hooks
echo "G65 recover drill passed."
