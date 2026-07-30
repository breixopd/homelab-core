"""Deploy monitoring and security agents to external hosts via Ansible."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from toolkit.core.ansible.ansible_inventory import write_inventory
from toolkit.core.ansible.ansible_runner import run_playbook_sync
from toolkit.core.async_utils import run_blocking
from toolkit.core.config.config import Config, ExternalHost, config_path, load_config


@dataclass
class ExternalDeployResult:
    success: bool
    message: str
    logs: list[str]


def _playbook_path(root: Path) -> Path:
    return root / "automation" / "ansible" / "playbooks" / "deploy-external-host.yml"


def deploy_external_host(
    root: Path,
    cfg: Config,
    host: ExternalHost,
    *,
    on_log: Callable[[str], None] | None = None,
) -> ExternalDeployResult:
    """Run Ansible playbook for one external host."""
    logs: list[str] = []

    def log(msg: str) -> None:
        logs.append(msg)
        if on_log:
            on_log(msg)

    if not host.services:
        return ExternalDeployResult(
            success=False,
            message=f"No services selected for '{host.name}'",
            logs=logs,
        )

    playbook = _playbook_path(root)
    if not playbook.is_file():
        return ExternalDeployResult(
            success=False,
            message="deploy-external-host.yml not found",
            logs=logs,
        )

    inventory = write_inventory(root, cfg)
    from toolkit.core.infra.hosts import host_integration_ansible_variables

    extra_vars = host_integration_ansible_variables(root, host)
    log(f"Deploying to {host.name} ({host.ip}): {', '.join(host.services)}")
    result = run_playbook_sync(
        root,
        playbook,
        inventory=inventory,
        limit=host.name,
        extra_vars=extra_vars,
        on_log=log,
    )
    if not result.ok:
        return ExternalDeployResult(
            success=False,
            message=f"Ansible failed for '{host.name}' (exit {result.returncode})",
            logs=logs,
        )
    return ExternalDeployResult(
        success=True,
        message=f"Deployed services to '{host.name}'",
        logs=logs,
    )


async def deploy_external_host_async(
    root: Path,
    host_name: str,
    *,
    on_log: Callable[[str], None] | None = None,
) -> ExternalDeployResult:
    cfg = load_config(config_path(root))
    host = next((h for h in cfg.external_hosts if h.name == host_name), None)
    if not host:
        return ExternalDeployResult(
            success=False,
            message=f"Host '{host_name}' not found",
            logs=[],
        )
    return await run_blocking(
        deploy_external_host,
        root,
        cfg,
        host,
        on_log=on_log,
    )
