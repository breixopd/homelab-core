from __future__ import annotations

from pathlib import Path

import yaml


def test_setup_compose_runs_controller_over_shared_private_socket() -> None:
    root = Path(__file__).parents[3]
    compose = yaml.safe_load((root / "docker-compose.setup.yml").read_text())
    services = compose["services"]

    assert compose["name"] == "homelab-setup"
    assert {"controller", "toolkit"}.issubset(services)
    assert services["toolkit"]["environment"]["HOMELAB_CONTROLLER_SOCKET"] == "/run/homelab-controller/controller.sock"
    assert (
        services["controller"]["environment"]["HOMELAB_CONTROLLER_SOCKET"] == "/run/homelab-controller/controller.sock"
    )
    assert "controller-socket:/run/homelab-controller:ro" in services["toolkit"]["volumes"]
    assert "controller-socket:/run/homelab-controller" in services["controller"]["volumes"]
    assert services["toolkit"]["ports"] == ["127.0.0.1:8080:8080"]
    assert services["toolkit"]["user"] == "10001:10001"
    assert services["controller"]["environment"]["HOMELAB_CONTROLLER_UI_GID"] == "10001"
    controller_volumes = services["controller"]["volumes"]
    assert all("/root/.ssh" not in volume for volume in controller_volumes)
    assert all("homelab-ssh" not in volume for volume in controller_volumes)
    assert "HOMELAB_SSH_KEY_FILE" not in services["controller"]["environment"]
    assert "PROXMOX_SSH_KEY_FILE" not in services["controller"]["environment"]
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in controller_volumes
    assert all("docker.sock" not in volume for volume in services["toolkit"]["volumes"])
    assert services["toolkit"].get("cap_add") is None
    assert all("/app/repo" not in volume for volume in services["toolkit"]["volumes"])
    assert all("ssh" not in volume.lower() for volume in services["toolkit"]["volumes"])
    assert services["toolkit"]["working_dir"] == "/app"
    assert services["toolkit"]["environment"]["HOMELAB_ROOT"] == "/run/homelab-ui-root"
    assert services["toolkit"]["environment"]["WEBUI_SECURE_COOKIES"] == "false"
