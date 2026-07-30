from __future__ import annotations

from pathlib import Path


def test_install_script_uses_repeatable_compose_topology() -> None:
    script = (Path(__file__).parents[3] / "scripts" / "install.sh").read_text()

    assert "ghcr.io/breixopd/homelab-toolkit:latest" in script
    assert "TOOLKIT_VERSION=" not in script
    assert 'docker image inspect "${TOOLKIT_IMAGE}"' in script
    assert 'TOOLKIT_IMAGE="${RESOLVED_IMAGE}"' in script
    assert "/opt/homelab-framework" in script
    assert ".homelab-framework-files.json" in script
    assert "toolkit.core.bootstrap.framework_sync" in script
    assert "Framework update failed; restarting the previous control plane" in script
    assert '[[ "${FRAMEWORK_SEEDED}" -eq 0 ]]' in script
    assert "Refusing to overwrite nonempty, unmanaged install root" in script
    assert 'COMPOSE_CANDIDATE="$(mktemp "${INSTALL_ROOT}/.docker-compose.bootstrap.XXXXXX")"' in script
    assert 'mv -f "${COMPOSE_CANDIDATE}" "${COMPOSE_FILE}"' in script
    assert 'if ! mkdir -p "${INSTALL_ROOT}" 2>/dev/null; then' in script
    assert 'if [[ ! -w "${INSTALL_ROOT}" ]]; then' in script
    assert "docker run -d" not in script
    assert 'docker compose -f "${COMPOSE_FILE}" up -d --force-recreate --wait' in script
    assert 'docker compose -f "${COMPOSE_CANDIDATE}" config --quiet' in script
    assert script.count("/var/run/docker.sock:/var/run/docker.sock:ro") == 1
    assert "no-new-privileges:true" in script
    assert '"127.0.0.1:8080:8080"' in script
    assert 'WEBUI_SECURE_COOKIES: "false"' in script
    assert "depends_on:" in script
    assert "condition: service_healthy" in script


def test_toolkit_image_contains_the_deployable_framework_snapshot() -> None:
    dockerfile = (Path(__file__).parents[3] / "toolkit" / "Dockerfile").read_text()

    assert "WORKDIR /opt/homelab-framework" in dockerfile
    for source in ("automation", "config", "infrastructure", "scripts", "stacks", "toolkit"):
        assert f"COPY {source}/ {source}/" in dockerfile
    assert "COPY docker-compose.example.yml docker-compose.yml" in dockerfile
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert ".homelab-framework-files.json" in dockerfile


def test_repository_ignores_local_package_build_output() -> None:
    patterns = (Path(__file__).parents[3] / ".gitignore").read_text().splitlines()

    assert "build/" in patterns
    assert "dist/" in patterns
