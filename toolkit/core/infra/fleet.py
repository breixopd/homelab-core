"""Fleet / VPS cluster management — Komodo Periphery onboarding and agent stack."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.ansible.ansible_inventory import write_inventory
from toolkit.core.config.config import (
    Config,
    ExternalHost,
    config_path,
    load_config,
    save_config,
)
from toolkit.core.config.mutations import configuration_mutation
from toolkit.core.infra.fleet_roles import fleet_default_services
from toolkit.core.infra.hosts import (
    HostIntegrationStatus,
    host_integration_ansible_variables,
    host_integration_statuses,
    managed_host_fingerprint,
    mark_host_reconciled,
    reconcile_host_integrations,
    test_host_connection,
    trust_host_key,
    with_host_integration_config,
)

if TYPE_CHECKING:
    from toolkit.core.deploy.operation_lease import OperationLease
from toolkit.core.infra.hosts import (
    remove_host as remove_external_host,
)

# Plain-host default (monitoring, wazuh, vpn, dns). Fleet nodes extend this
# with ldap-client via fleet_default_services().
_FLEET_SERVICES = fleet_default_services()


@dataclass
class FleetOnboardResult:
    success: bool
    message: str
    logs: list[str]


@dataclass
class FleetNodeStatus:
    name: str
    ssh_ok: bool
    agents: tuple[HostIntegrationStatus, ...]
    reconciled: bool

    def agent(self, integration: str) -> HostIntegrationStatus | None:
        return next((status for status in self.agents if status.name == integration), None)


@dataclass(slots=True)
class _FleetOnboardingContext:
    """Core-owned execution boundary for service-owned onboarding hooks."""

    config: Config
    host: ExternalHost
    root: Path
    variables: Mapping[str, object]
    _log: Callable[[str], None]
    _retry_roles: Callable[[tuple[str, ...]], bool]

    def log(self, message: str) -> None:
        self._log(message)

    def retry_integrations(self, integrations: tuple[str, ...]) -> bool:
        from toolkit.core.infra.fleet_roles import ansible_roles_for_services

        roles = tuple(ansible_roles_for_services(list(integrations), kind=self.host.kind, cfg=self.config))
        if not roles:
            self.log(f"Fleet retry skipped: no Ansible roles declared for {', '.join(integrations)}")
            return False
        return self._retry_roles(roles)


def list_nodes(root: Path) -> list[ExternalHost]:
    cfg = load_config(config_path(root))
    return [host for host in cfg.external_hosts if host.kind == "fleet"]


def add_node(
    root: Path,
    name: str,
    ip: str,
    *,
    ssh_user: str = "root",
    ssh_port: int = 22,
    cluster_group: str = "",
    lldap_email: str = "",
    headscale_tags: list[str] | None = None,
    services: list[str] | None = None,
    integrations: dict[str, dict[str, bool | int | float | str]] | None = None,
) -> ExternalHost:
    node = ExternalHost(
        name=name,
        ip=ip,
        kind="fleet",
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        cluster_group=cluster_group.strip(),
        lldap_email=lldap_email.strip(),
        headscale_tags=list(headscale_tags or []),
        services=list(services) if services is not None else list(_FLEET_SERVICES),
        integrations=integrations or {},
    )
    with configuration_mutation(root, "fleet-node-add"):
        cfg = load_config(config_path(root))
        existing = next((host for host in cfg.external_hosts if host.name == name), None)
        if existing is not None and existing.kind == "fleet":
            raise ValueError(f"Fleet node '{name}' already exists")
        if existing is not None:
            merged = list(existing.services)
            for service in node.services:
                if service not in merged:
                    merged.append(service)
            node = ExternalHost.model_validate(
                {
                    **node.model_dump(mode="python"),
                    "services": merged,
                    "integrations": {**existing.integrations, **node.integrations},
                }
            )
            cfg.external_hosts = [node if host.name == name else host for host in cfg.external_hosts]
        else:
            cfg.external_hosts.append(node)
        save_config(with_host_integration_config(cfg, previous=existing, current=node), config_path(root))
    return node


def remove_node(root: Path, name: str) -> bool:
    if get_node(root, name) is None:
        return False
    return remove_external_host(root, name)


def get_node(root: Path, name: str) -> ExternalHost | None:
    return next((n for n in list_nodes(root) if n.name == name), None)


def _playbook_path(root: Path) -> Path:
    return root / "automation" / "ansible" / "playbooks" / "onboard-fleet-node.yml"


def _onboard_node_impl(
    root: Path,
    name: str,
    *,
    on_log: Callable[[str], None] | None = None,
    operation_lease: OperationLease,
) -> FleetOnboardResult:
    """Run full fleet onboarding playbook for one node."""
    logs: list[str] = []

    def log(msg: str) -> None:
        logs.append(msg)
        if on_log:
            on_log(msg)

    node = get_node(root, name)
    if not node:
        return FleetOnboardResult(success=False, message=f"Fleet node '{name}' not found", logs=logs)
    expected_fingerprint = managed_host_fingerprint(node)

    playbook = _playbook_path(root)
    if not playbook.is_file():
        return FleetOnboardResult(success=False, message="onboard-fleet-node.yml not found", logs=logs)

    cfg = load_config(config_path(root))
    post_sync_warnings: list[str] = []
    for line in trust_host_key(node, root=root):
        log(line)

    integration_result = reconcile_host_integrations(root, node)
    for line in integration_result.logs:
        log(line)
    post_sync_warnings.extend(integration_result.errors)

    from toolkit.services import enabled_service_plugins

    plugins = [plugin for _, plugin in enabled_service_plugins(cfg) if plugin.selected_for_fleet_host(node)]
    service_variables: dict[str, object] = {}
    try:
        for plugin in plugins:
            contribution = plugin.prepare_fleet_onboarding(cfg, node, root)
            collisions = set(service_variables).intersection(contribution.variables)
            if collisions:
                raise RuntimeError(
                    f"fleet onboarding variable collision: {', '.join(sorted(collisions))} (service {plugin.service})"
                )
            service_variables.update(contribution.variables)
            for line in contribution.logs:
                log(line)
    except Exception as exc:
        return FleetOnboardResult(
            success=False,
            message=f"Fleet service preparation failed for '{name}': {exc}",
            logs=logs,
        )

    inventory = write_inventory(root, cfg)
    extra: dict[str, object] = dict(service_variables)
    integration_variables = host_integration_ansible_variables(root, node)
    collisions = set(extra).intersection(integration_variables)
    if collisions:
        return FleetOnboardResult(
            success=False,
            message=f"Fleet integration variable collision: {', '.join(sorted(collisions))}",
            logs=logs,
        )
    extra.update(integration_variables)

    from toolkit.core.ansible.ansible_runner import run_playbook_sync

    log(f"Onboarding fleet node {node.name} ({node.ip})")
    result = run_playbook_sync(
        root,
        playbook,
        inventory=inventory,
        limit=node.name,
        extra_vars=extra,
        on_log=log,
    )
    if not result.ok:
        return FleetOnboardResult(
            success=False,
            message=f"Onboarding failed for '{name}' (exit {result.returncode})",
            logs=logs,
        )

    if integration_result.refresh_nodes:
        from toolkit.core.deploy.deploy_workflow import run_deploy_workflow

        log(f"Refreshing runtime nodes for managed-host desired state: {', '.join(integration_result.refresh_nodes)}")
        refresh = asyncio.run(
            run_deploy_workflow(
                root,
                load_config(config_path(root)),
                on_log=log,
                on_step=lambda step, state: log(f"Runtime refresh: {step} -> {state}"),
                skip_infra=True,
                skip_dns=True,
                targets=integration_result.refresh_nodes,
                operation_lease=operation_lease,
            )
        )
        if not refresh.success:
            post_sync_warnings.append("managed-host runtime refresh failed")

    def retry_roles(roles: tuple[str, ...]) -> bool:
        log(f"Fleet retry: {', '.join(roles)}")
        retry = run_playbook_sync(
            root,
            playbook,
            inventory=inventory,
            limit=node.name,
            extra_vars=extra,
            extra_args=["--tags", ",".join(roles)],
            on_log=log,
        )
        return retry.ok

    context = _FleetOnboardingContext(cfg, node, root, extra, log, retry_roles)
    for plugin in plugins:
        try:
            plugin.after_fleet_onboarding(context)
        except Exception as exc:
            post_sync_warnings.append(f"{plugin.service}: post-onboarding reconciliation failed ({exc})")
            log(post_sync_warnings[-1])

    msg = f"Onboarded fleet node '{name}'"
    if node.cluster_group:
        msg += f" (cluster group: {node.cluster_group})"
    if post_sync_warnings:
        return FleetOnboardResult(
            success=False,
            message=f"Fleet host integrations need attention ({len(post_sync_warnings)} error(s))",
            logs=logs,
        )
    if not mark_host_reconciled(root, name, expected_fingerprint):
        return FleetOnboardResult(
            success=False,
            message="Fleet desired state changed during onboarding; retry reconciliation",
            logs=logs,
        )
    return FleetOnboardResult(success=True, message=msg, logs=logs)


def onboard_node(
    root: Path,
    name: str,
    *,
    on_log: Callable[[str], None] | None = None,
    operation_lease: OperationLease | None = None,
) -> FleetOnboardResult:
    """Run full fleet onboarding while owning the mutating-operation boundary."""
    from toolkit.core.deploy.operation_lease import OperationLease

    lease = operation_lease or OperationLease.acquire(root, "fleet-onboard")
    owns_lease = operation_lease is None
    try:
        lease.assert_owns_root(root)
        lease.raise_if_cancelled()
        return _onboard_node_impl(root, name, on_log=on_log, operation_lease=lease)
    finally:
        if owns_lease:
            lease.release()


def node_status(root: Path, name: str) -> FleetNodeStatus | None:
    node = get_node(root, name)
    if not node:
        return None
    ssh_ok = test_host_connection(node, root=root)
    return FleetNodeStatus(
        name=node.name,
        ssh_ok=ssh_ok,
        agents=host_integration_statuses(root, node, probe=ssh_ok),
        reconciled=node.reconciled,
    )


def all_node_statuses(root: Path) -> list[FleetNodeStatus]:
    return [s for n in list_nodes(root) if (s := node_status(root, n.name)) is not None]
