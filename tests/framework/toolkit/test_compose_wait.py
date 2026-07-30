"""Tests for compose_wait polling."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from toolkit.core.deploy.compose_wait import DEFAULT_INTERVAL, DEFAULT_POLLS, wait_for_compose_status


def test_default_wait_budget_allows_large_manifest_driven_nodes() -> None:
    assert DEFAULT_POLLS * DEFAULT_INTERVAL >= 2 * 60 * 60


@pytest.fixture
def root(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
domain: test.local
proxmox:
  host: 192.0.2.10
  provision_machines: true
vms:
  infra: {enabled: true, ip: 10.10.10.10}
"""
    )
    return tmp_path


def test_wait_returns_ok_on_first_poll(root: Path) -> None:
    with patch("toolkit.core.deploy.compose_wait.load_config") as mock_cfg:
        mock_cfg.return_value.vm_ip.return_value = "10.10.10.10"
        with patch("toolkit.core.deploy.compose_wait.ssh_run_on_vm", return_value=(0, "ok\n", "")):
            assert wait_for_compose_status(root, "infra", max_polls=3, interval=0) == 0


def test_wait_returns_failed_on_status_file(root: Path) -> None:
    with patch("toolkit.core.deploy.compose_wait.load_config") as mock_cfg:
        mock_cfg.return_value.vm_ip.return_value = "10.10.10.10"
        with patch(
            "toolkit.core.deploy.compose_wait.ssh_run_on_vm",
            side_effect=[(0, "failed", ""), (0, "log line", "")],
        ):
            assert wait_for_compose_status(root, "infra", max_polls=1, interval=0) == 1


def test_wait_shell_quotes_operator_controlled_repo_destination(root: Path) -> None:
    commands: list[str] = []

    def run(_cfg, _ip, command, **_kwargs):
        commands.append(command)
        return (0, "failed" if len(commands) == 1 else "log", "")

    with patch("toolkit.core.deploy.compose_wait.load_config") as mock_cfg:
        mock_cfg.return_value.vm_ip.return_value = "10.10.10.10"
        with patch("toolkit.core.deploy.compose_wait.ssh_run_on_vm", side_effect=run):
            assert (
                wait_for_compose_status(
                    root,
                    "infra",
                    repo_dest="/opt/homelab; touch /tmp/injected",
                    max_polls=1,
                    interval=0,
                )
                == 1
            )

    assert commands[0].startswith("cat '/opt/homelab; touch /tmp/injected/")
    assert commands[1].startswith("tail -40 '/opt/homelab; touch /tmp/injected/")


def test_wait_returns_failed_on_wave_failure_log(root: Path) -> None:
    with patch("toolkit.core.deploy.compose_wait.load_config") as mock_cfg:
        mock_cfg.return_value.vm_ip.return_value = "10.10.10.10"
        with patch(
            "toolkit.core.deploy.compose_wait.ssh_run_on_vm",
            side_effect=[
                (0, "failed", ""),
                (0, "[staggered-up] WARN: wave failure for media\n", ""),
            ],
        ):
            assert wait_for_compose_status(root, "media", max_polls=1, interval=0) == 1


def test_wait_detects_stale_running_marker(root: Path) -> None:
    with patch("toolkit.core.deploy.compose_wait.load_config") as mock_cfg:
        mock_cfg.return_value.vm_ip.return_value = "10.10.10.10"
        with patch(
            "toolkit.core.deploy.compose_wait.ssh_run_on_vm",
            side_effect=[(0, "running", "")] * 2 + [(0, "dead", ""), (0, "log tail", "")],
        ):
            assert wait_for_compose_status(root, "infra", max_polls=10, interval=0) == 1


def test_wait_detects_stale_log_mtime(root: Path) -> None:
    with patch("toolkit.core.deploy.compose_wait.load_config") as mock_cfg:
        mock_cfg.return_value.vm_ip.return_value = "10.10.10.10"
        with patch(
            "toolkit.core.deploy.compose_wait.ssh_run_on_vm",
            side_effect=[
                (0, "running", ""),
                (0, "running", ""),
                (0, "alive", ""),
                (0, "600", ""),
                (0, "600", ""),
                (0, "stale log\n", ""),
            ],
        ):
            assert wait_for_compose_status(root, "infra", max_polls=10, interval=0) == 1


def test_detached_launcher_does_not_treat_its_own_shell_as_a_running_deploy() -> None:
    playbook = (Path(__file__).parents[3] / "automation/ansible/playbooks/deploy-server-toolkit.yml").read_text(
        encoding="utf-8"
    )
    guard = playbook.split('if [[ "${HOMELAB_FORCE_COMPOSE:-0}" != "1" ]]', 1)[1].split(
        'systemctl stop "homelab-staggered-',
        1,
    )[0]

    assert "COMPOSE_RUNNING=0" in guard
    assert '[[ "$pid" != "$$" && "$pid" != "$PPID" ]]' in guard
    assert '[[ "$COMPOSE_RUNNING" == "1" ]]' in guard
    assert 'if pgrep -f "[h]omelab-toolkit' not in guard
