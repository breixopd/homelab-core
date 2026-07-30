#!/usr/bin/env bash
set -euo pipefail

SOPS_VERSION="3.13.2"
AGE_VERSION="1.3.1"
INSTALL_BIN_DIR="${CI_TOOL_BIN_DIR:-/usr/local/bin}"

case "$(uname -m)" in
  x86_64)
    ARCH="amd64"
    SOPS_SHA256="154dfe4cd70554bdd82b98e4cd4acf191d43d01ead6f00a73477aa44c4ac42ef"
    AGE_SHA256="bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377"
    ;;
  aarch64|arm64)
    ARCH="arm64"
    SOPS_SHA256="78abf2e15c86250a1553ae6f53aba96be6b2a8126f160b1534959add3467ad76"
    AGE_SHA256="c6878a324421b69e3e20b00ba17c04bc5c6dab0030cfe55bf8f68fa8d9e9093a"
    ;;
  *)
    echo "Unsupported architecture for CI secret tools: $(uname -m)" >&2
    exit 1
    ;;
esac

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

download() {
  local url="$1"
  local destination="$2"
  curl --fail --silent --show-error --location \
    --proto '=https' --tlsv1.2 --retry 4 --retry-all-errors \
    --output "$destination" "$url"
}

SOPS_ASSET="sops-v${SOPS_VERSION}.linux.${ARCH}"
download "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/${SOPS_ASSET}" "$WORK_DIR/$SOPS_ASSET"
printf '%s  %s\n' "$SOPS_SHA256" "$WORK_DIR/$SOPS_ASSET" | sha256sum --check --status

AGE_ASSET="age-v${AGE_VERSION}-linux-${ARCH}.tar.gz"
download "https://github.com/FiloSottile/age/releases/download/v${AGE_VERSION}/${AGE_ASSET}" "$WORK_DIR/$AGE_ASSET"
printf '%s  %s\n' "$AGE_SHA256" "$WORK_DIR/$AGE_ASSET" | sha256sum --check --status
tar --extract --gzip --file "$WORK_DIR/$AGE_ASSET" --directory "$WORK_DIR"

install_binary() {
  if [[ -w "$INSTALL_BIN_DIR" ]]; then
    install -m 0755 "$1" "$INSTALL_BIN_DIR/$2"
  else
    sudo install -m 0755 "$1" "$INSTALL_BIN_DIR/$2"
  fi
}

install_binary "$WORK_DIR/$SOPS_ASSET" sops
install_binary "$WORK_DIR/age/age" age
install_binary "$WORK_DIR/age/age-keygen" age-keygen

SOPS_DISABLE_VERSION_CHECK=1 "$INSTALL_BIN_DIR/sops" --version
"$INSTALL_BIN_DIR/age-keygen" --version
