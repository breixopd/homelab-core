from __future__ import annotations

from unittest.mock import patch

from toolkit.core.config.config import Config
from toolkit.core.infra.ssh_probe import probe_ssh_connectivity, ssh_ok


def test_ssh_connectivity_reports_failure(tmp_path):
    cfg = Config(domain="example.com", ssh={"key_file": "/tmp/key"})
    with (
        patch("toolkit.core.infra.ssh_probe.resolve_ansible_ssh_key", return_value=tmp_path / "k"),
        patch("toolkit.core.infra.ssh_probe.ssh_run_on_vm", return_value=(255, "", "Permission denied")),
        patch("toolkit.core.infra.ssh_probe.refresh_known_hosts_file", return_value=[]),
    ):
        (tmp_path / "k").write_text("fake")
        lines = probe_ssh_connectivity(cfg, tmp_path)
    assert any("FAIL infra" in line for line in lines)


def test_ssh_connectivity_reports_ok(tmp_path):
    cfg = Config(domain="example.com", ssh={"key_file": "/tmp/key"})
    with (
        patch("toolkit.core.infra.ssh_probe.resolve_ansible_ssh_key", return_value=tmp_path / "k"),
        patch("toolkit.core.infra.ssh_probe.ssh_run_on_vm", return_value=(0, "infra-01\n", "")),
        patch("toolkit.core.infra.ssh_probe.refresh_known_hosts_file", return_value=["known_hosts: trusted infra"]),
    ):
        (tmp_path / "k").write_text("fake")
        lines = probe_ssh_connectivity(cfg, tmp_path)
    assert any("OK infra" in line for line in lines)


def test_targeted_ssh_connectivity_does_not_probe_unrelated_nodes(tmp_path):
    cfg = Config(domain="example.com", ssh={"key_file": "/tmp/key"})
    addresses: list[str] = []

    def probe(_cfg, ip, *_args, **_kwargs):
        addresses.append(ip)
        return 0, "media-01\n", ""

    with (
        patch("toolkit.core.infra.ssh_probe.resolve_ansible_ssh_key", return_value=tmp_path / "k"),
        patch("toolkit.core.infra.ssh_probe.ssh_run_on_vm", side_effect=probe),
        patch("toolkit.core.infra.ssh_probe.refresh_known_hosts_file", return_value=[]),
    ):
        (tmp_path / "k").write_text("fake")
        lines = probe_ssh_connectivity(cfg, tmp_path, targets=("media",))

    assert addresses == [cfg.node_ip("media")]
    assert any("OK media" in line for line in lines)


# ── ssh_ok ────────────────────────────────────────────────────────────────────


def test_ssh_ok_returns_true_when_every_enabled_vm_succeeds(tmp_path):
    """Healthy fleet → True. The informational `using key …` line must NOT poison all()."""
    cfg = Config(domain="example.com", ssh={"key_file": "/tmp/key"})
    with (
        patch("toolkit.core.infra.ssh_probe.resolve_ansible_ssh_key", return_value=tmp_path / "k"),
        patch("toolkit.core.infra.ssh_probe.ssh_run_on_vm", return_value=(0, "infra-01\n", "")),
        patch("toolkit.core.infra.ssh_probe.refresh_known_hosts_file", return_value=[]),
    ):
        (tmp_path / "k").write_text("fake")
        assert ssh_ok(cfg, tmp_path) is True


def test_ssh_ok_returns_false_when_any_vm_unreachable(tmp_path):
    """One failing VM → False (the historical alert-storm state)."""
    cfg = Config(domain="example.com", ssh={"key_file": "/tmp/key"})
    with (
        patch("toolkit.core.infra.ssh_probe.resolve_ansible_ssh_key", return_value=tmp_path / "k"),
        patch("toolkit.core.infra.ssh_probe.ssh_run_on_vm", return_value=(255, "", "Permission denied")),
        patch("toolkit.core.infra.ssh_probe.refresh_known_hosts_file", return_value=[]),
    ):
        (tmp_path / "k").write_text("fake")
        assert ssh_ok(cfg, tmp_path) is False


def test_ssh_ok_returns_false_when_some_ok_some_fail(tmp_path):
    """Mixed OK/FAIL across VMs → False (a partial fleet is not a healthy fleet)."""
    cfg = Config(domain="example.com", ssh={"key_file": "/tmp/key"})
    mixed = [(0, "infra-01\n", ""), (255, "", "timed out"), (0, "apps-01\n", "")]

    def dispatch(cfg, ip, *args, **kwargs):
        return mixed.pop(0)

    with (
        patch("toolkit.core.infra.ssh_probe.resolve_ansible_ssh_key", return_value=tmp_path / "k"),
        patch("toolkit.core.infra.ssh_probe.ssh_run_on_vm", side_effect=dispatch),
        patch("toolkit.core.infra.ssh_probe.refresh_known_hosts_file", return_value=[]),
    ):
        (tmp_path / "k").write_text("fake")
        assert ssh_ok(cfg, tmp_path) is False


def test_ssh_ok_returns_false_when_no_ssh_key(tmp_path):
    """Missing guest SSH key means the probe cannot connect."""
    cfg = Config(domain="example.com", ssh={"key_file": "/tmp/key"})
    with patch("toolkit.core.infra.ssh_probe.resolve_ansible_ssh_key", return_value=None):
        assert ssh_ok(cfg, tmp_path) is False
