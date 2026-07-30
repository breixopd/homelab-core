from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def _role_defaults(role: str) -> dict:
    return yaml.safe_load((ROOT / "automation" / "ansible" / "roles" / role / "defaults" / "main.yml").read_text())


def test_tailscale_role_uses_versioned_vendor_archives_with_pinned_checksums() -> None:
    defaults = _role_defaults("vpn_client")
    tasks = (ROOT / "automation" / "ansible" / "roles" / "vpn_client" / "tasks" / "main.yml").read_text()
    mesh = (ROOT / "toolkit" / "services" / "headscale" / "mesh.py").read_text()

    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", defaults["tailscale_version"])
    assert set(defaults["tailscale_releases"]) == {"x86_64", "aarch64"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", release["sha256"]) for release in defaults["tailscale_releases"].values())
    assert "https://pkgs.tailscale.com/stable/tailscale_" in tasks
    assert 'checksum: "sha256:' in tasks
    assert "tailscale.com/install.sh" not in tasks
    assert "tailscale.com/install.sh" not in mesh
    assert "rerun the managed guest deployment" in mesh


def test_komodo_role_installs_digest_verified_release_without_remote_script_execution() -> None:
    defaults = _role_defaults("komodo_periphery")
    role = ROOT / "automation" / "ansible" / "roles" / "komodo_periphery"
    tasks = (role / "tasks" / "main.yml").read_text()
    config_template = (role / "templates" / "periphery.config.toml.j2").read_text()
    service_template = (role / "templates" / "periphery.service.j2").read_text()
    core_compose = (ROOT / "toolkit" / "services" / "komodo-core" / "compose.yaml").read_text()

    version = defaults["komodo_periphery_version"]
    assert f"ghcr.io/moghtech/komodo-core:{version}" in core_compose
    assert set(defaults["komodo_periphery_releases"]) == {"x86_64", "aarch64"}
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", release["sha256"]) for release in defaults["komodo_periphery_releases"].values()
    )
    assert "https://github.com/moghtech/komodo/releases/download/v" in tasks
    assert 'checksum: "sha256:' in tasks
    assert "setup-periphery.py" not in tasks
    assert "raw.githubusercontent.com" not in tasks
    assert "onboarding_key" in config_template
    assert "UMask=0077" in service_template
    assert 'mode: "0600"' in tasks


def test_guest_komodo_bootstrap_imports_manifest_owned_module() -> None:
    task = (ROOT / "toolkit" / "services" / "komodo-core" / "ansible" / "post-deploy.yml").read_text()

    assert 'importlib.import_module("toolkit.services.komodo-core.bootstrap")' in task
    assert "from toolkit.core.bootstrap.komodo_bootstrap import" not in task
    assert '- "{{ ansible_playbook_python }}"' in task
    assert "{{ homelab_controller_root }}/.venv/bin/python3" not in task
