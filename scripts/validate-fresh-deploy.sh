#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT=""
DRY_RUN=0
CONFIRM_REMOTE_WIPE=0

usage() {
  cat <<'EOF'
Usage: scripts/validate-fresh-deploy.sh [--root PATH] [--dry-run] [--confirm-remote-wipe]

Runs all local quality gates first. Remote destruction and redeployment are
performed only with --confirm-remote-wipe and only after an isolated restore
drill has issued a fresh verified checkpoint.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --confirm-remote-wipe) CONFIRM_REMOTE_WIPE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ROOT="$(cd "$ROOT" && pwd)"
cd "$ROOT"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
EVIDENCE_DIR="$ROOT/.homelab-state/validation/$RUN_ID"
RESULTS="$EVIDENCE_DIR/results.tsv"
mkdir -p "$EVIDENCE_DIR"
: > "$RESULTS"

CONTROLLER_PID=""
CONTROLLER_RUNTIME="/tmp/homelab-controller-$RUN_ID"
CONTROLLER_RAW_LOG="$EVIDENCE_DIR/controller.raw.log"
CONTROLLER_LOG="$EVIDENCE_DIR/controller.log"

redact() {
  sed -E \
    -e 's/((TOKEN|PASSWORD|SECRET|PRIVATE_KEY|API_KEY)[A-Za-z0-9_ -]*[=:])[[:space:]]*[^[:space:]]+/\1[REDACTED]/Ig' \
    -e 's/(PVEAPIToken=)[^[:space:]]+/\1[REDACTED]/g' \
    -e 's/(Authorization:)[[:space:]]*[^[:space:]]+/\1 [REDACTED]/Ig'
}

cleanup_controller() {
  if [[ -n "$CONTROLLER_PID" ]] && kill -0 "$CONTROLLER_PID" 2>/dev/null; then
    kill "$CONTROLLER_PID" 2>/dev/null || true
    wait "$CONTROLLER_PID" 2>/dev/null || true
  fi
  if [[ -f "$CONTROLLER_RAW_LOG" ]]; then
    redact <"$CONTROLLER_RAW_LOG" >"$CONTROLLER_LOG"
    : >"$CONTROLLER_RAW_LOG"
  fi
  rm -rf "$CONTROLLER_RUNTIME"
}

trap cleanup_controller EXIT

start_local_controller() {
  install -d -m 0700 "$CONTROLLER_RUNTIME"
  export HOMELAB_ROOT="$ROOT"
  export HOMELAB_CONTROLLER_DB="$CONTROLLER_RUNTIME/controller.db"
  export HOMELAB_CONTROLLER_PAYLOAD_KEY_FILE="$CONTROLLER_RUNTIME/payload.key"
  export HOMELAB_CONTROLLER_SOCKET="$CONTROLLER_RUNTIME/controller.sock"
  export HOMELAB_CONTROLLER_ROLE="local"
  export HOMELAB_CONTROLLER_TOKEN_FILE="$CONTROLLER_RUNTIME/local.token"
  export HOMELAB_CONTROLLER_LOCAL_TOKEN_FILE="$CONTROLLER_RUNTIME/local.token"
  export HOMELAB_CONTROLLER_UI_TOKEN_FILE="$CONTROLLER_RUNTIME/ui.token"
  unset HOMELAB_CONTROLLER_UI_GID

  "$ROOT/.venv/bin/python" -m toolkit.controller >"$CONTROLLER_RAW_LOG" 2>&1 &
  CONTROLLER_PID=$!
  for _ in $(seq 1 120); do
    if ! kill -0 "$CONTROLLER_PID" 2>/dev/null; then
      echo "Local controller exited during startup" >&2
      return 1
    fi
    if [[ -S "$HOMELAB_CONTROLLER_SOCKET" && -f "$HOMELAB_CONTROLLER_TOKEN_FILE" ]] && \
      "$ROOT/.venv/bin/python" -c \
        'from toolkit.controller.client import controller_client_from_environment as f; c=f(); h=c.health(); c.close(); assert h.status == "ok"' \
        >/dev/null 2>&1; then
      echo "Local controller is healthy (pid=$CONTROLLER_PID)"
      return 0
    fi
    sleep 0.5
  done
  echo "Local controller did not become healthy within 60 seconds" >&2
  return 1
}

run_gate() {
  local name="$1"
  shift
  local raw="$EVIDENCE_DIR/$name.raw.log"
  local log="$EVIDENCE_DIR/$name.log"
  local started ended rc
  started="$(date +%s)"
  echo "==> $name"
  set +e
  "$@" >"$raw" 2>&1
  rc=$?
  set -e
  redact <"$raw" >"$log"
  : >"$raw"
  ended="$(date +%s)"
  printf '%s\t%s\t%s\t%s\n' "$name" "$rc" "$((ended - started))" "$log" >>"$RESULTS"
  if [[ $rc -ne 0 ]]; then
    echo "FAILED: $name (see $log)" >&2
    return "$rc"
  fi
  echo "PASS: $name"
}

write_report() {
  local final_status="$1"
  VALIDATION_STATUS="$final_status" VALIDATION_RUN_ID="$RUN_ID" \
    VALIDATION_RESULTS="$RESULTS" VALIDATION_EVIDENCE_DIR="$EVIDENCE_DIR" \
    "$ROOT/.venv/bin/python" - <<'PY'
import csv
import json
import os
from pathlib import Path

results_path = Path(os.environ["VALIDATION_RESULTS"])
evidence_dir = Path(os.environ["VALIDATION_EVIDENCE_DIR"])
rows = []
with results_path.open(encoding="utf-8") as stream:
    for name, rc, duration, log in csv.reader(stream, delimiter="\t"):
        rows.append({
            "name": name,
            "ok": int(rc) == 0,
            "exit_code": int(rc),
            "duration_seconds": int(duration),
            "log": str(Path(log).relative_to(evidence_dir)),
        })
document = {
    "run_id": os.environ["VALIDATION_RUN_ID"],
    "status": os.environ["VALIDATION_STATUS"],
    "gates": rows,
}
(evidence_dir / "report.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
lines = [
    f"# Fresh Deploy Validation {document['run_id']}",
    "",
    f"Status: **{document['status']}**",
    "",
    "| Gate | Result | Duration | Log |",
    "|---|---:|---:|---|",
]
for row in rows:
    result = "PASS" if row["ok"] else "FAIL"
    lines.append(f"| {row['name']} | {result} | {row['duration_seconds']}s | `{row['log']}` |")
(evidence_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

stop_failed() {
  write_report "failed"
  echo "Validation stopped. Evidence: $EVIDENCE_DIR" >&2
  exit 1
}

export ANSIBLE_LOCAL_TEMP=/tmp/homelab-ansible-local
export ANSIBLE_REMOTE_TEMP=/tmp/homelab-ansible-remote

run_gate ruff-check "$ROOT/.venv/bin/ruff" check toolkit tests/framework tests/services || stop_failed
run_gate ruff-format "$ROOT/.venv/bin/ruff" format --check toolkit tests/framework tests/services || stop_failed
run_gate mypy "$ROOT/.venv/bin/mypy" --ignore-missing-imports toolkit/core toolkit/cli toolkit/webui toolkit/controller || stop_failed
run_gate framework-tests "$ROOT/.venv/bin/pytest" tests/framework -q --timeout=60 || stop_failed
run_gate ui-smoke "$ROOT/.venv/bin/pytest" tests/e2e/test_ui_pages.py -q --timeout=60 || stop_failed
run_gate generate "$ROOT/.venv/bin/homelab-toolkit" --root "$ROOT" generate || stop_failed
run_gate ansible-syntax "$ROOT/.venv/bin/ansible-playbook" --syntax-check \
  automation/ansible/site.yml -i automation/ansible/inventory/hosts.yml \
  --extra-vars "@$ROOT/automation/ansible/group_vars/generated.yml" || stop_failed
run_gate tofu-validate tofu -chdir=infrastructure validate -no-color || stop_failed

if [[ $DRY_RUN -eq 1 ]]; then
  write_report "local-gates-passed"
  echo "Dry run complete. Remote mutation was not attempted. Evidence: $EVIDENCE_DIR"
  exit 0
fi

if [[ $CONFIRM_REMOTE_WIPE -ne 1 ]]; then
  write_report "awaiting-remote-confirmation"
  echo "Local gates passed. Re-run with --confirm-remote-wipe to validate the remote deployment." >&2
  echo "Evidence: $EVIDENCE_DIR"
  exit 2
fi

TOOL=("$ROOT/.venv/bin/homelab-toolkit" --root "$ROOT")
run_gate pre-wipe-dump "${TOOL[@]}" maintenance dump || stop_failed
run_gate dump-inventory "${TOOL[@]}" maintenance list-dumps --json || stop_failed
DUMP_ID="$("$ROOT/.venv/bin/python" -c \
  'import json,sys; rows=json.load(open(sys.argv[1])); print(rows[0]["dump_id"] if rows else "")' \
  "$EVIDENCE_DIR/dump-inventory.log")"
if [[ -z "$DUMP_ID" ]]; then
  echo "No discovered dump was available for the required restore drill." >&2
  stop_failed
fi
run_gate pre-wipe-restore-drill "${TOOL[@]}" maintenance restore-drill "$DUMP_ID" || stop_failed
run_gate controller-start start_local_controller || stop_failed
run_gate remote-redeploy "${TOOL[@]}" deploy all --destroy-first --yes || stop_failed
run_gate deployment-verify "${TOOL[@]}" deploy verify || stop_failed
run_gate hook-verify "${TOOL[@]}" deploy verify --hooks || stop_failed
run_gate external-probe "${TOOL[@]}" deploy verify --external || stop_failed
run_gate post-deploy-dump "${TOOL[@]}" maintenance dump || stop_failed
run_gate post-deploy-dump-inventory "${TOOL[@]}" maintenance list-dumps --json || stop_failed
POST_DUMP_ID="$("$ROOT/.venv/bin/python" -c \
  'import json,sys; rows=json.load(open(sys.argv[1])); print(rows[0]["dump_id"] if rows else "")' \
  "$EVIDENCE_DIR/post-deploy-dump-inventory.log")"
if [[ -z "$POST_DUMP_ID" ]]; then
  echo "No post-deploy dump was available for the restore drill." >&2
  stop_failed
fi
run_gate post-deploy-restore-drill "${TOOL[@]}" maintenance restore-drill "$POST_DUMP_ID" || stop_failed

write_report "passed"
echo "Fresh deploy validation passed. Evidence: $EVIDENCE_DIR"
