"""Headscale owns mesh-client installation on managed guests."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_managed_guests_install_mesh_binary_before_headscale_runtime_hook() -> None:
    guest_setup = (ROOT / "automation" / "ansible" / "guest-setup.yml").read_text()
    mesh_hook = (ROOT / "toolkit" / "services" / "headscale" / "ansible" / "vpn.yml").read_text()

    install_index = mesh_hook.index("Install the mesh client on its manifest-selected routing node")
    dispatch_index = guest_setup.index("Apply manifest-selected service guest task file")
    hooks_index = guest_setup.index("Run final post-start hooks")
    assert "service_guest_task_files" in guest_setup[dispatch_index:hooks_index]
    assert "name: vpn_client" in mesh_hook[install_index:]
