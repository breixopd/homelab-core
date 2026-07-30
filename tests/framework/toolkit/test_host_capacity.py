from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from toolkit.core.config.config import Config, HostCapacityConfig
from toolkit.core.infra.host_capacity import (
    _CAP_CACHE,
    configured_capacity_estimate,
    detect_host_capacity,
    detect_proxmox_capacity,
    resolve_proxmox_host,
)


def test_detect_host_capacity_small_vm():
    cap = detect_host_capacity(cpu_cores=2, mem_total_kb=2_000_000, load_1m=1.0)
    assert cap.cpu_cores == 2
    assert cap.mem_total_mb < 4096
    assert cap.wave_timeout_s >= 180
    assert cap.max_pull_parallel >= 1


def test_configured_capacity_estimate_is_offline_and_uses_declared_threshold():
    cfg = Config(
        domain="example.com",
        host_capacity=HostCapacityConfig(cpu_cores=16, mem_total_mb=64000, load_threshold=12),
    )

    cap = configured_capacity_estimate(cfg)

    assert cap is not None
    assert cap.source == "configured-offline"
    assert cap.cpu_cores == 16
    assert cap.mem_total_mb == 64000
    assert cap.load_1m == 0.0
    assert cap.load_threshold == 12.0


def test_configured_capacity_estimate_requires_both_resource_values():
    cfg = Config(domain="example.com", host_capacity=HostCapacityConfig(cpu_cores=16))

    assert configured_capacity_estimate(cfg) is None


def test_detect_host_capacity_overloaded():
    cap = detect_host_capacity(cpu_cores=4, mem_total_kb=16_000_000, load_1m=20.0)
    assert cap.overloaded
    assert cap.warning_message() is not None
    assert cap.inter_wave_sleep_s >= 10


def test_resolve_proxmox_host_from_dns_public_ip():
    cfg = Config(domain="example.com", dns={"public_ip": "192.0.2.10"})
    assert resolve_proxmox_host(cfg) == "192.0.2.10"


def test_resolve_proxmox_host_override():
    cfg = Config(
        domain="example.com",
        host_capacity=HostCapacityConfig(proxmox_host="10.0.0.1"),
    )
    assert resolve_proxmox_host(cfg) == "10.0.0.1"


@patch("toolkit.core.infra.host_capacity.subprocess.run")
def test_detect_proxmox_capacity_ssh(mock_run, tmp_path: Path):
    mock_run.return_value = MagicMock(returncode=0, stdout="8\n32768000\n1.25\n")
    key = tmp_path / "proxmox-key"
    key.write_text("test-private-key\n")
    cfg = Config(
        domain="example.com",
        dns={"public_ip": "192.0.2.10"},
        proxmox={"ssh": {"user": "operator", "port": 2222, "key_file": str(key)}},
    )
    result = detect_proxmox_capacity(cfg)
    assert result == (8, 32768000, 1.25)
    command = mock_run.call_args.args[0]
    assert "operator@192.0.2.10" in command
    assert command[command.index("-p") + 1] == "2222"


@patch.dict("os.environ", {"HOMELAB_NODE": "infra"}, clear=False)
@patch("toolkit.core.infra.host_capacity.detect_proxmox_capacity")
def test_detect_host_capacity_skips_remote_on_guest(mock_remote):
    mock_remote.return_value = (32, 64_000_000, 0.5)
    cfg = Config(
        domain="example.com",
        proxmox={"provision_machines": True},
        host_capacity=HostCapacityConfig(cpu_cores=8, mem_total_mb=32000),
    )
    cap = detect_host_capacity(cfg=cfg, root=Path("/tmp"))
    mock_remote.assert_not_called()
    # Guest LXC: use local /proc, not remote Proxmox or static config overrides.
    assert cap.source == "local-infra"
    assert cap.cpu_cores > 0


@patch("toolkit.core.infra.host_capacity.detect_proxmox_capacity")
def test_detect_host_capacity_uses_proxmox_when_provisioning(mock_remote):
    mock_remote.return_value = (8, 32_000_000, 2.0)
    cfg = Config(domain="example.com", proxmox={"provision_machines": True})
    cap = detect_host_capacity(cfg=cfg, root=Path("/tmp"))
    assert cap.source == "proxmox"
    assert cap.cpu_cores == 8


@patch("toolkit.core.infra.host_capacity.detect_lxc_capacity")
@patch("toolkit.core.infra.host_capacity.detect_proxmox_capacity")
def test_detect_host_capacity_falls_back_to_lxc(mock_proxmox, mock_lxc):
    _CAP_CACHE.clear()
    mock_proxmox.return_value = None
    mock_lxc.return_value = (4, 16_000_000, 1.0)
    cfg = Config(domain="example.com", proxmox={"provision_machines": True})
    cap = detect_host_capacity(cfg=cfg, root=Path("/tmp"))
    assert cap.source == "lxc"
    assert cap.cpu_cores == 4


@patch("toolkit.core.infra.host_capacity.detect_lxc_capacity")
@patch("toolkit.core.infra.host_capacity.detect_proxmox_capacity")
def test_detect_host_capacity_raises_when_unreachable(mock_proxmox, mock_lxc):
    _CAP_CACHE.clear()
    mock_proxmox.return_value = None
    mock_lxc.return_value = None
    cfg = Config(domain="example.com", proxmox={"provision_machines": True})
    with pytest.raises(RuntimeError, match="Cannot detect host capacity"):
        detect_host_capacity(cfg=cfg, root=Path("/tmp"), fast=False)


@patch("toolkit.core.infra.host_capacity.detect_lxc_capacity")
@patch("toolkit.core.infra.host_capacity.detect_proxmox_capacity")
def test_detect_host_capacity_fast_fallback_when_unreachable(mock_proxmox, mock_lxc):
    _CAP_CACHE.clear()
    mock_proxmox.return_value = None
    mock_lxc.return_value = None
    cfg = Config(domain="example.com", proxmox={"provision_machines": True})
    cap = detect_host_capacity(cfg=cfg, root=Path("/tmp"), fast=True)
    assert cap.source == "local-fast-fallback"
    assert cap.cpu_cores > 0


def test_config_override_cpu_and_mem():
    cfg = Config(
        domain="example.com",
        host_capacity=HostCapacityConfig(cpu_cores=16, mem_total_mb=64000, use_proxmox_host=False),
    )
    cap = detect_host_capacity(cfg=cfg)
    assert cap.cpu_cores == 16
    assert cap.mem_total_mb == 64000
    assert cap.source == "config"
