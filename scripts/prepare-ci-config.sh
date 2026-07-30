#!/usr/bin/env bash
# Create local-only SSH and age identities required by deployment-model validation.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_PARENT="${RUNNER_TEMP:-$REPO_ROOT/.homelab-state}"
mkdir -p "$KEY_PARENT"
KEY_DIR="$(mktemp -d "$KEY_PARENT/homelab-ci-key.XXXXXX")"
chmod 0700 "$KEY_DIR"

ssh-keygen -q -t ed25519 -N "" -C "homelab-ci@invalid" -f "$KEY_DIR/operator"
PUBLIC_KEY="$(<"$KEY_DIR/operator.pub")"

AGE_KEY_FILE="$KEY_DIR/age.key"
age-keygen -o "$AGE_KEY_FILE" >/dev/null
chmod 0600 "$AGE_KEY_FILE"
AGE_RECIPIENT="$(age-keygen -y "$AGE_KEY_FILE")"
cat >"$REPO_ROOT/.sops.yaml" <<EOF
creation_rules:
  - path_regex: .*\\.yaml$
    age: $AGE_RECIPIENT
EOF
export SOPS_AGE_KEY_FILE="$AGE_KEY_FILE"
if [[ -n "${GITHUB_ENV:-}" ]]; then
  printf 'SOPS_AGE_KEY_FILE=%s\n' "$AGE_KEY_FILE" >>"$GITHUB_ENV"
fi

if [[ ! -f "$REPO_ROOT/config.yaml" ]]; then
  uv run --locked homelab-toolkit --root "$REPO_ROOT" config init
fi

uv run --locked homelab-toolkit --root "$REPO_ROOT" config set \
  "dns.public_ip=192.0.2.10" \
  "proxmox.ssh_public_key=$PUBLIC_KEY" \
  "proxmox.ssh.key_file=$KEY_DIR/operator"
