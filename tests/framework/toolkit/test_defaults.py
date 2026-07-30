from __future__ import annotations

from toolkit.core.config.config import (
    DEFAULT_LXC_TEMPLATE_SHA256,
    DEFAULT_LXC_TEMPLATE_URL,
    DEFAULT_PROXMOX_NODE,
    Config,
)


def test_default_proxmox_node():
    assert DEFAULT_PROXMOX_NODE == "pve"


def test_default_lxc_template_url_starts_with_http():
    assert DEFAULT_LXC_TEMPLATE_URL.startswith("http")


def test_default_lxc_template_url_contains_debian():
    assert "debian" in DEFAULT_LXC_TEMPLATE_URL.lower()
    assert "12.12-1" in DEFAULT_LXC_TEMPLATE_URL


def test_default_lxc_template_checksum_is_sha256():
    assert DEFAULT_LXC_TEMPLATE_SHA256 == "ff5c55cba730fc1e93bc7de3e0ea4aecb05c692094009cfcf2999973a56f15e5"


def test_default_machine_declares_its_gateway():
    cfg = Config()

    assert cfg.machines[cfg.control_node].gateway == "10.10.10.1"


def test_default_machine_declares_its_network_prefix():
    cfg = Config()

    assert cfg.machines[cfg.control_node].cidr == 24


def test_default_proxmox_node_used_in_config():
    from toolkit.core.config.config import ProxmoxConfig

    cfg = ProxmoxConfig()
    assert cfg.node == DEFAULT_PROXMOX_NODE
