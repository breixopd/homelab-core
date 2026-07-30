"""Tests for portable Ansible inventory generation."""

from __future__ import annotations

from pathlib import Path

from toolkit.core.ansible.ansible_inventory import render_inventory, resolve_node_host_ip, write_inventory
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.machines import MachineSpec


def test_render_inventory_uses_arbitrary_machine_plugins(tmp_path: Path, monkeypatch):
    cfg = Config(
        domain="lab.test",
        dns={"public_ip": "203.0.113.10"},
        proxmox={"ssh": {"user": "pve-operator", "port": 2202, "key_file": "/keys/pve"}},
        machines={
            "control-east": MachineSpec(
                hostname="control-01",
                address="10.10.10.20",
                gateway="10.10.10.1",
                vmid=820,
                labels=("control",),
            ),
            "worker-west": MachineSpec(
                kind="vm",
                hostname="worker-07",
                address="10.10.10.27",
                gateway="10.10.10.1",
                vmid=827,
                labels=("compute",),
                admin_user="operator",
                ssh_port=2222,
                cloud_image_datastore="local",
                cloud_image_format="qcow2",
                cloud_image_url="https://images.example.test/debian.qcow2",
                cloud_image_sha256="c" * 64,
            ),
        },
    )
    save_config(cfg, config_path(tmp_path))
    key = tmp_path / "id_test"
    key.write_text("fake-key\n")
    proxmox_key = tmp_path / "keys" / "pve"
    proxmox_key.parent.mkdir()
    proxmox_key.write_text("fake-proxmox-key\n")
    cfg.proxmox.ssh.user = "pve-operator"
    cfg.proxmox.ssh.port = 2202
    cfg.proxmox.ssh.key_file = str(proxmox_key)
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_inventory.resolve_ansible_ssh_key",
        lambda _cfg, _root: key,
    )
    data = render_inventory(cfg, tmp_path)
    assert data["all"]["vars"]["base_domain"] == "lab.test"
    control = data["all"]["children"]["control-east"]["hosts"]["control-01"]
    assert control["ansible_host"] == "10.10.10.20"
    assert control["homelab_node_id"] == "control-east"
    assert control["homelab_machine_labels"] == ["control"]
    worker = data["all"]["children"]["worker-west"]["hosts"]["worker-07"]
    assert worker["ansible_user"] == "operator"
    assert worker["ansible_port"] == 2222
    assert "ProxyCommand" in worker["ansible_ssh_common_args"]
    assert "IdentityAgent=none" in worker["ansible_ssh_common_args"]
    assert "pve-operator@203.0.113.10" in worker["ansible_ssh_common_args"]
    assert "-p 2202" in worker["ansible_ssh_common_args"]
    proxmox = data["all"]["children"]["proxmox_hosts"]["hosts"]["pve-01"]
    assert proxmox["ansible_user"] == "pve-operator"
    assert proxmox["ansible_port"] == 2202
    assert proxmox["ansible_ssh_private_key_file"] == str(proxmox_key.resolve())


def test_render_inventory_uses_direct_guest_access_from_managed_node(tmp_path: Path, monkeypatch):
    cfg = Config(domain="lab.test", dns={"public_ip": "203.0.113.10"})
    key = tmp_path / "ssh" / "homelab_admin_ed25519"
    key.parent.mkdir()
    key.write_text("fake-key\n")
    monkeypatch.setenv("HOMELAB_NODE", cfg.control_node)
    monkeypatch.setattr("toolkit.core.ansible.ansible_inventory._is_directly_reachable", lambda *_args: True)

    data = render_inventory(cfg, tmp_path)

    guest = data["all"]["children"]["apps"]["hosts"][cfg.machines["apps"].hostname]
    assert f"IdentityFile={key}" in guest["ansible_ssh_common_args"]
    assert "ProxyCommand" not in guest["ansible_ssh_common_args"]


def test_render_inventory_keeps_proxy_access_from_controller_container(tmp_path: Path, monkeypatch):
    cfg = Config(domain="lab.test", dns={"public_ip": "203.0.113.10"})
    key = tmp_path / "ssh" / "homelab_admin_ed25519"
    key.parent.mkdir()
    key.write_text("fake-key\n")
    monkeypatch.setenv("HOMELAB_NODE", cfg.control_node)
    monkeypatch.setattr("toolkit.core.ansible.ansible_inventory._is_directly_reachable", lambda *_args: False)

    data = render_inventory(cfg, tmp_path)

    guest = data["all"]["children"]["infra"]["hosts"][cfg.machines["infra"].hostname]
    assert "ProxyCommand" in guest["ansible_ssh_common_args"]
    assert f"IdentityFile={key}" in guest["ansible_ssh_common_args"]


def test_render_inventory_declares_empty_external_group_without_hosts(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(domain="lab.test", dns={"public_ip": "203.0.113.10"})
    monkeypatch.setenv("HOMELAB_NODE", cfg.control_node)
    monkeypatch.setattr("toolkit.core.ansible.ansible_inventory._is_directly_reachable", lambda *_args: True)

    data = render_inventory(cfg, tmp_path)

    assert data["all"]["children"]["external_hosts"] == {"hosts": {}}


def test_render_inventory_includes_external_hosts(tmp_path: Path, monkeypatch):
    from toolkit.core.config.config import ExternalHost

    cfg = Config(
        domain="lab.test",
        dns={"public_ip": "203.0.113.10"},
        external_hosts=[
            ExternalHost(
                name="nas-01",
                ip="192.168.1.50",
                ssh_user="root",
                ssh_port=22,
                services=["monitoring-agent"],
            ),
        ],
    )
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_inventory.resolve_ansible_ssh_key",
        lambda _cfg, _root: None,
    )
    data = render_inventory(cfg, tmp_path)
    ext = data["all"]["children"]["external_hosts"]["hosts"]["nas-01"]
    assert ext["ansible_host"] == "192.168.1.50"
    assert "monitoring-agent" in ext["external_services"]
    assert ext["external_service_roles"] == ["monitoring_agent"]
    assert "ProxyCommand" not in str(ext)


def test_render_inventory_omits_roles_owned_by_disabled_services(tmp_path: Path, monkeypatch):
    from toolkit.core.config.config import ExternalHost, ServicesConfig

    cfg = Config(
        domain="lab.test",
        dns={"public_ip": "203.0.113.10"},
        services=ServicesConfig(management=True, security=False),
        external_hosts=[
            ExternalHost(
                name="vps-01",
                ip="192.168.1.50",
                kind="fleet",
                services=["komodo-periphery", "vpn-client", "monitoring-agent"],
            ),
        ],
    )
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_inventory.resolve_ansible_ssh_key",
        lambda _cfg, _root: None,
    )

    data = render_inventory(cfg, tmp_path)
    roles = data["all"]["children"]["external_hosts"]["hosts"]["vps-01"]["external_service_roles"]

    assert roles == ["komodo_periphery", "monitoring_agent"]


def test_write_inventory_creates_file(tmp_path: Path, monkeypatch):
    cfg = Config(domain="x.test", dns={"public_ip": "1.2.3.4"})
    save_config(cfg, config_path(tmp_path))
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_inventory.resolve_ansible_ssh_key",
        lambda _cfg, _root: None,
    )
    out = write_inventory(tmp_path, cfg, proxmox_only=True)
    assert out.is_file()
    assert "pve-01" in out.read_text()


def test_write_inventory_preserves_pinned_known_hosts(tmp_path: Path, monkeypatch) -> None:
    """Routine inventory writes must never rotate or erase SSH trust entries."""
    from toolkit.core.config.config import ExternalHost

    cfg = Config(
        domain="x.test",
        dns={"public_ip": "1.2.3.4"},
        external_hosts=[ExternalHost(name="nas-01", ip="192.0.2.50")],
    )
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_inventory.resolve_ansible_ssh_key",
        lambda _cfg, _root: None,
    )

    def unexpected_refresh(*_args, **_kwargs):
        raise AssertionError("routine writes must not refresh host keys")

    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_inventory._refresh_known_hosts",
        unexpected_refresh,
    )

    known_hosts = tmp_path / "automation/ansible/inventory/known_hosts"
    known_hosts.parent.mkdir(parents=True)
    pinned = "|1|salt|hash ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIpinned\n"
    known_hosts.write_text(pinned)

    write_inventory(tmp_path, cfg, proxmox_only=True)

    assert known_hosts.read_text() == pinned


def test_resolve_node_host_ip_uses_declared_hostname(tmp_path: Path) -> None:
    inventory = tmp_path / "automation/ansible/inventory/hosts.yml"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        """all:
  children:
    worker-west:
      hosts:
        worker-07:
          ansible_host: 10.10.10.27
"""
    )

    assert resolve_node_host_ip(tmp_path, "worker-west") == "10.10.10.27"
