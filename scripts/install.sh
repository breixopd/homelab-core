#!/usr/bin/env bash
set -euo pipefail

# Homelab Toolkit bootstrap — either runs the toolkit container (default) or
# delegates to the local CLI for CI smoke tests / offline generation.
#
# Usage:
#   curl -fsSL .../install.sh | bash
#   ./scripts/install.sh --preset all --smoke-test --yes   # CI / compose validation

TOOLKIT_IMAGE="${TOOLKIT_IMAGE:-ghcr.io/breixopd/homelab-toolkit:latest}"
INSTALL_ROOT="${HOMELAB_ROOT:-/opt/homelab}"
# Path inside the toolkit container (must match HOMELAB_ROOT env there)
CONTAINER_HOMELAB_ROOT="${CONTAINER_HOMELAB_ROOT:-/opt/homelab}"

err() { echo "ERROR: $1" >&2; exit 1; }

# Parse optional flags (first invocation only)
SMOKE_TEST=0
PRESET=""
YES=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --smoke-test) SMOKE_TEST=1; shift ;;
        --preset)
            [[ -n "${2:-}" ]] || err "--preset requires a value"
            PRESET="$2"
            shift 2
            ;;
        --yes|-y) YES=1; shift ;;
        *) err "Unknown option: $1" ;;
    esac
done

if [[ "$SMOKE_TEST" -eq 1 ]]; then
    command -v uv >/dev/null 2>&1 || err "uv is required for --smoke-test: https://docs.astral.sh/uv/"
    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    ST_ROOT="${HOMELAB_SMOKE_ROOT:-${REPO_ROOT}/.smoke-test}"
    mkdir -p "${ST_ROOT}"
    export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}"
    uv sync --locked --no-dev --no-install-project
    ARGS=(uv run --no-sync python -m toolkit.cli --root "${ST_ROOT}" install)
    [[ -n "$PRESET" ]] && ARGS+=(--preset "$PRESET")
    ARGS+=(--smoke-test)
    [[ "$YES" -eq 1 ]] && ARGS+=(--yes)
    exec "${ARGS[@]}"
fi

command -v docker >/dev/null 2>&1 || err "Docker is required. Install: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || err "Docker Compose v2 is required."

echo "==> Creating ${INSTALL_ROOT}"
if ! mkdir -p "${INSTALL_ROOT}" 2>/dev/null; then
    command -v sudo >/dev/null 2>&1 || err "Cannot create ${INSTALL_ROOT}; choose a writable HOMELAB_ROOT."
    sudo mkdir -p "${INSTALL_ROOT}"
fi
if [[ ! -w "${INSTALL_ROOT}" ]]; then
    command -v sudo >/dev/null 2>&1 || err "Cannot write ${INSTALL_ROOT}; choose a writable HOMELAB_ROOT."
    sudo chown "$(id -u):$(id -g)" "${INSTALL_ROOT}"
fi

echo "==> Pulling ${TOOLKIT_IMAGE}"
docker pull "${TOOLKIT_IMAGE}" >/dev/null || err "Unable to pull ${TOOLKIT_IMAGE}. Authenticate to its registry and retry."

RESOLVED_IMAGE="$(
    docker image inspect "${TOOLKIT_IMAGE}" \
        --format '{{range .RepoDigests}}{{println .}}{{end}}' | sed -n '1p'
)"
if [[ -z "${RESOLVED_IMAGE}" ]] && [[ "${TOOLKIT_IMAGE}" == *@sha256:* ]]; then
    RESOLVED_IMAGE="${TOOLKIT_IMAGE}"
fi
[[ "${RESOLVED_IMAGE}" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || \
    err "Registry did not resolve ${TOOLKIT_IMAGE} to an immutable image digest."
TOOLKIT_IMAGE="${RESOLVED_IMAGE}"
echo "==> Using immutable toolkit image ${TOOLKIT_IMAGE}"

FRAMEWORK_MODE=""
FRAMEWORK_SEEDED=0
if [[ -d "${INSTALL_ROOT}/.git" ]] && \
   [[ -f "${INSTALL_ROOT}/pyproject.toml" ]] && \
   [[ -d "${INSTALL_ROOT}/toolkit/services" ]] && \
   [[ -d "${INSTALL_ROOT}/automation" ]] && \
   [[ -d "${INSTALL_ROOT}/infrastructure" ]]; then
    echo "==> Using existing framework checkout in ${INSTALL_ROOT}"
    FRAMEWORK_MODE="source"
elif [[ -f "${INSTALL_ROOT}/.homelab-framework-version" ]] && \
     [[ -f "${INSTALL_ROOT}/.homelab-framework-files.json" ]]; then
    FRAMEWORK_MODE="managed"
elif [[ -n "$(find "${INSTALL_ROOT}" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    err "Refusing to overwrite nonempty, unmanaged install root ${INSTALL_ROOT}. Set HOMELAB_ROOT to an empty directory, a source checkout, or a managed framework snapshot."
else
    echo "==> Seeding framework snapshot into ${INSTALL_ROOT}"
    docker run --rm \
        --user "$(id -u):$(id -g)" \
        --volume "${INSTALL_ROOT}:/target" \
        --entrypoint sh \
        "${TOOLKIT_IMAGE}" \
        -ec 'cp -R /opt/homelab-framework/. /target/'
    FRAMEWORK_MODE="managed"
    FRAMEWORK_SEEDED=1
fi
[[ -f "${INSTALL_ROOT}/.homelab-framework-files.json" || -d "${INSTALL_ROOT}/.git" ]] || \
    err "Framework seed validation failed for ${INSTALL_ROOT}."

if [[ "${FRAMEWORK_MODE}" == "managed" ]] && [[ "${FRAMEWORK_SEEDED}" -eq 0 ]]; then
    CONTROL_PLANE_STOPPED=0
    if [[ -f "${INSTALL_ROOT}/docker-compose.bootstrap.yml" ]]; then
        echo "==> Stopping the control plane for a transactional framework update"
        docker compose -f "${INSTALL_ROOT}/docker-compose.bootstrap.yml" stop || \
            err "Unable to stop the existing control plane safely."
        CONTROL_PLANE_STOPPED=1
    fi
    if ! docker run --rm \
        --user "$(id -u):$(id -g)" \
        --volume "${INSTALL_ROOT}:/target" \
        --entrypoint python3 \
        "${TOOLKIT_IMAGE}" \
        -m toolkit.core.bootstrap.framework_sync \
        --source /opt/homelab-framework \
        --target /target; then
        if [[ "${CONTROL_PLANE_STOPPED}" -eq 1 ]]; then
            echo "Framework update failed; restarting the previous control plane" >&2
            docker compose -f "${INSTALL_ROOT}/docker-compose.bootstrap.yml" up -d --wait --wait-timeout 120 || true
        fi
        err "Framework update was rolled back."
    fi
fi

echo "==> Writing managed bootstrap topology"
COMPOSE_FILE="${INSTALL_ROOT}/docker-compose.bootstrap.yml"
COMPOSE_CANDIDATE="$(mktemp "${INSTALL_ROOT}/.docker-compose.bootstrap.XXXXXX")"
trap 'rm -f "${COMPOSE_CANDIDATE:-}"' EXIT
SSH_VOLUME_LINE=""
if [[ -d "${HOME}/.ssh" ]]; then
    SSH_DIR="${HOME}/.ssh"
    if [[ -n "${HOMELAB_SSH_KEY_FILE:-}" ]] && [[ -f "${HOMELAB_SSH_KEY_FILE}" ]]; then
        SSH_DIR="$(dirname "${HOMELAB_SSH_KEY_FILE}")"
    fi
    SSH_VOLUME_LINE="      - \"${SSH_DIR}:/root/.ssh:ro\""
fi
SSH_AUTH_SOCK="${SSH_AUTH_SOCK:-}"
AGENT_VOLUME_LINE=""
AGENT_ENV_LINE=""
if [[ -n "${SSH_AUTH_SOCK}" ]] && [[ -S "${SSH_AUTH_SOCK}" ]]; then
    AGENT_VOLUME_LINE="      - \"${SSH_AUTH_SOCK}:/ssh-agent:ro\""
    AGENT_ENV_LINE="      SSH_AUTH_SOCK: /ssh-agent"
fi

cat > "${COMPOSE_CANDIDATE}" <<EOF
name: homelab-bootstrap
services:
  controller:
    image: "${TOOLKIT_IMAGE}"
    restart: unless-stopped
    read_only: true
    cap_drop: [ALL]
    cap_add: [CHOWN, DAC_OVERRIDE, FOWNER]
    security_opt: [no-new-privileges:true]
    tmpfs:
      - /tmp
      - /root/.ansible
    volumes:
      - "${INSTALL_ROOT}:${CONTAINER_HOMELAB_ROOT}"
      - controller-data:/var/lib/homelab-controller
      - controller-payload-key:/run/secrets/homelab-controller
      - controller-runtime:/run/homelab-controller
      - /var/run/docker.sock:/var/run/docker.sock:ro
${SSH_VOLUME_LINE}
${AGENT_VOLUME_LINE}
    environment:
      HOMELAB_ROOT: "${CONTAINER_HOMELAB_ROOT}"
      PYTHONPATH: "${CONTAINER_HOMELAB_ROOT}"
      HOMELAB_CONTROLLER_DB: /var/lib/homelab-controller/controller.db
      HOMELAB_CONTROLLER_PAYLOAD_KEY_FILE: /run/secrets/homelab-controller/payload.key
      HOMELAB_CONTROLLER_SOCKET: /run/homelab-controller/controller.sock
      HOMELAB_CONTROLLER_ROLE: local
      HOMELAB_CONTROLLER_TOKEN_FILE: /var/lib/homelab-controller/local.token
      HOMELAB_CONTROLLER_LOCAL_TOKEN_FILE: /var/lib/homelab-controller/local.token
      HOMELAB_CONTROLLER_UI_TOKEN_FILE: /run/homelab-controller/ui.token
      HOMELAB_CONTROLLER_UI_GID: "10001"
      HOME: /tmp
      ANSIBLE_LOCAL_TMP: /tmp
${AGENT_ENV_LINE}
    working_dir: "${CONTAINER_HOMELAB_ROOT}"
    entrypoint: [python3]
    command: [-m, toolkit.controller]
    healthcheck:
      test: [CMD, python3, -c, "from toolkit.controller.client import controller_client_from_environment as f; c=f(); h=c.health(); c.close(); assert h.status == 'ok'"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 10s
    pids_limit: 256
  toolkit:
    image: "${TOOLKIT_IMAGE}"
    user: "10001:10001"
    restart: unless-stopped
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    tmpfs:
      - /tmp
      - /run/homelab-ui-root
    volumes:
      - controller-runtime:/run/homelab-controller:ro
      - homelab-ui-state:/var/lib/homelab-ui
    environment:
      HOMELAB_ROOT: /run/homelab-ui-root
      HOMELAB_CONTROLLER_SOCKET: /run/homelab-controller/controller.sock
      HOMELAB_CONTROLLER_ROLE: ui
      HOMELAB_CONTROLLER_TOKEN_FILE: /run/homelab-controller/ui.token
      XDG_STATE_HOME: /var/lib/homelab-ui
      WEBUI_SESSION_SECRET_FILE: /var/lib/homelab-ui/webui-secret
      WEBUI_SECURE_COOKIES: "false"
      HOME: /tmp
    ports:
      - "127.0.0.1:8080:8080"
    depends_on:
      controller:
        condition: service_healthy
    command: [ui]
    healthcheck:
      test: [CMD, python3, -c, "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 10s
    pids_limit: 128
volumes:
  controller-data:
  controller-payload-key:
  controller-runtime:
  homelab-ui-state:
EOF

echo "==> Validating and reconciling toolkit services"
docker compose -f "${COMPOSE_CANDIDATE}" config --quiet
mv -f "${COMPOSE_CANDIDATE}" "${COMPOSE_FILE}"
COMPOSE_CANDIDATE=""
docker compose -f "${COMPOSE_FILE}" up -d --force-recreate --wait --wait-timeout 120

echo ""
echo "Homelab Toolkit is running at http://localhost:8080"
echo "Run 'docker compose -f ${COMPOSE_FILE} exec controller homelab-toolkit bootstrap token' to authorize first-run setup."

echo ""
echo "Done."
