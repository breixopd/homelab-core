from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from toolkit.core.config.config import Config, ExternalHost
from toolkit.core.deploy.external_deploy import (
    ExternalDeployResult,
    deploy_external_host,
    deploy_external_host_async,
)
from toolkit.core.infra.fleet_roles import FLEET_SERVICE_CATALOG


def _host(**kwargs) -> ExternalHost:
    defaults = dict(name="nas-01", ip="10.0.0.50", services=["monitoring-agent"])
    defaults.update(kwargs)
    return ExternalHost(**defaults)


def test_external_host_playbook_covers_plain_remote_agent_catalog() -> None:
    playbook = (
        Path(__file__).parents[4] / "automation" / "ansible" / "playbooks" / "deploy-external-host.yml"
    ).read_text()
    remote_roles = {
        service.ansible_role for service in FLEET_SERVICE_CATALOG if "plain" in service.kinds and service.ansible_role
    }

    assert "external_service_roles" in playbook
    assert "ansible.builtin.include_role" in playbook
    for role in remote_roles:
        assert role in {service.ansible_role for service in FLEET_SERVICE_CATALOG}


def test_deploy_external_host_no_services(tmp_path: Path):
    cfg = Config(domain="example.com")
    host = _host(services=[])

    result = deploy_external_host(tmp_path, cfg, host)

    assert result == ExternalDeployResult(
        success=False,
        message="No services selected for 'nas-01'",
        logs=[],
    )


def test_deploy_external_host_missing_playbook(tmp_path: Path):
    cfg = Config(domain="example.com")
    host = _host()

    result = deploy_external_host(tmp_path, cfg, host)

    assert not result.success
    assert "not found" in result.message


def test_deploy_external_host_ansible_failure(tmp_path: Path):
    root = tmp_path / "homelab"
    playbook = root / "automation" / "ansible" / "playbooks" / "deploy-external-host.yml"
    playbook.parent.mkdir(parents=True)
    playbook.write_text("---\n- hosts: all\n  tasks: []\n")
    cfg = Config(domain="example.com")
    host = _host()
    logs: list[str] = []

    proc = MagicMock(returncode=2, pid=1234)
    proc.communicate.return_value = ("PLAY RECAP **", "fatal error")
    with (
        patch("toolkit.core.deploy.external_deploy.write_inventory", return_value=root / "inv"),
        patch("toolkit.core.ansible.ansible_runner.resolve_tool", return_value="/usr/bin/ansible-playbook"),
        patch("toolkit.core.ansible.ansible_runner.generated_extra_vars", return_value=["-e", "x=1"]),
        patch("toolkit.core.ansible.ansible_runner.subprocess.Popen", return_value=proc),
    ):
        result = deploy_external_host(root, cfg, host, on_log=logs.append)

    assert not result.success
    assert "exit 2" in result.message
    assert any("fatal error" in line for line in logs)


def test_deploy_external_host_success(tmp_path: Path):
    root = tmp_path / "homelab"
    playbook = root / "automation" / "ansible" / "playbooks" / "deploy-external-host.yml"
    playbook.parent.mkdir(parents=True)
    playbook.write_text("---\n- hosts: all\n  tasks: []\n")
    cfg = Config(domain="example.com")
    host = _host(services=["monitoring-agent", "wazuh-agent"])

    proc = MagicMock(returncode=0, pid=1234)
    proc.communicate.return_value = ("changed=1", "")
    with (
        patch("toolkit.core.deploy.external_deploy.write_inventory", return_value=root / "inv"),
        patch("toolkit.core.ansible.ansible_runner.resolve_tool", return_value="/usr/bin/ansible-playbook"),
        patch("toolkit.core.ansible.ansible_runner.generated_extra_vars", return_value=[]),
        patch("toolkit.core.ansible.ansible_runner.subprocess.Popen", return_value=proc) as mock_run,
    ):
        result = deploy_external_host(root, cfg, host)

    assert result.success
    assert "Deployed services" in result.message
    cmd = mock_run.call_args[0][0]
    assert "--limit" in cmd
    assert "nas-01" in cmd


def test_deploy_backup_storage_passes_only_restricted_public_key(tmp_path: Path):
    root = tmp_path / "homelab"
    playbook = root / "automation" / "ansible" / "playbooks" / "deploy-external-host.yml"
    playbook.parent.mkdir(parents=True)
    playbook.write_text("---\n- hosts: all\n  tasks: []\n")
    cfg = Config(domain="example.com")
    host = _host(
        services=["backup-storage"],
        integrations={"backup-storage": {"path": "/srv/kopia"}},
    )
    identity = MagicMock(public_key="ssh-ed25519 AAAArestricted homelab-kopia-backup")
    proc = MagicMock(returncode=0, pid=1234)
    proc.communicate.return_value = ("changed=1", "")
    captured_vars: dict[str, str] = {}

    def run_ansible(command, **_kwargs):
        var_file = next(Path(str(part)[1:]) for part in command if str(part).startswith("@"))
        captured_vars.update(yaml.safe_load(var_file.read_text(encoding="utf-8")))
        return proc

    with (
        patch("toolkit.core.deploy.external_deploy.write_inventory", return_value=root / "inv"),
        patch("toolkit.core.ops.backup_ssh.ensure_backup_ssh_identity", return_value=identity),
        patch("toolkit.core.ansible.ansible_runner.resolve_tool", return_value="/usr/bin/ansible-playbook"),
        patch("toolkit.core.ansible.ansible_runner.generated_extra_vars", return_value=[]),
        patch("toolkit.core.ansible.ansible_runner.subprocess.Popen", side_effect=run_ansible) as run,
    ):
        result = deploy_external_host(root, cfg, host)

    assert result.success
    command = run.call_args.args[0]
    assert captured_vars["kopia_backup_public_key"] == "ssh-ed25519 AAAArestricted homelab-kopia-backup"
    assert "AAArestricted" not in " ".join(command)
    assert "PRIVATE KEY" not in " ".join(command)


def test_deploy_external_host_async_host_not_found(tmp_path: Path):
    from toolkit.core.config.config import save_config
    from toolkit.core.config.storage import config_path

    root = tmp_path / "homelab"
    root.mkdir()
    save_config(Config(domain="example.com"), config_path(root))

    result = asyncio.run(deploy_external_host_async(root, "missing-host"))

    assert not result.success
    assert "not found" in result.message


def test_deploy_external_host_async_success(tmp_path: Path):
    from toolkit.core.config.config import save_config
    from toolkit.core.config.storage import config_path

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = Config(
        domain="example.com",
        external_hosts=[_host(name="edge", services=["monitoring-agent"])],
    )
    save_config(cfg, config_path(root))
    expected = ExternalDeployResult(success=True, message="ok", logs=["done"])

    with patch("toolkit.core.deploy.external_deploy.deploy_external_host", return_value=expected):
        result = asyncio.run(deploy_external_host_async(root, "edge"))

    assert result.success
    assert result.logs == ["done"]
