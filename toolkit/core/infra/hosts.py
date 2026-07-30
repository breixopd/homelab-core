from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from toolkit.core.config.config import (
    Config,
    ExternalHost,
    external_host_default_services,
    load_config,
    save_config,
)
from toolkit.core.config.mutations import configuration_lock, configuration_mutation
from toolkit.core.config.storage import config_path


@dataclass(frozen=True, slots=True)
class HostIntegrationReconcileResult:
    logs: tuple[str, ...]
    errors: tuple[str, ...]
    refresh_nodes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class HostIntegrationStatus:
    """One manifest-selected host integration's current agent state."""

    name: str
    label: str
    active: bool | None
    detail: str


def managed_host_fingerprint(host: ExternalHost) -> str:
    """Return a stable fingerprint for entity-scoped optimistic concurrency."""
    payload = json.dumps(
        host.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def with_host_integration_config(
    cfg: Config,
    *,
    previous: ExternalHost | None,
    current: ExternalHost | None,
) -> Config:
    """Apply every service-owned desired-state hook for a host mutation."""
    updated = cfg.model_copy(deep=True)
    from toolkit.core.infra.fleet_roles import FLEET_SERVICE_CATALOG
    from toolkit.services import get_service_plugin

    owners = dict.fromkeys(integration.owner for integration in FLEET_SERVICE_CATALOG)
    for owner in owners:
        plugin = get_service_plugin(owner)
        if plugin is None:
            raise RuntimeError(f"host integration owner {owner!r} has no service plugin")
        updated = plugin.configure_host_integrations(updated, previous=previous, current=current)
    return updated


def add_host(
    root: Path,
    name: str,
    ip: str,
    ssh_user: str = "root",
    ssh_port: int = 22,
    services: list[str] | None = None,
    integrations: dict[str, dict[str, bool | int | float | str]] | None = None,
) -> ExternalHost:
    host = ExternalHost(
        name=name,
        ip=ip,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        services=list(services) if services is not None else external_host_default_services(),
        integrations=integrations or {},
    )
    with configuration_mutation(root, "managed-host-add"):
        cfg = load_config(config_path(root))
        if any(item.name == host.name for item in cfg.external_hosts):
            raise ValueError(f"Managed host '{host.name}' already exists")
        cfg.external_hosts.append(host)
        save_config(with_host_integration_config(cfg, previous=None, current=host), config_path(root))
    return host


def reconcile_host_integrations(
    root: Path,
    host: ExternalHost,
    *,
    on_log: Callable[[str], None] | None = None,
) -> HostIntegrationReconcileResult:
    """Apply controller-side integrations derived from a managed host record."""
    logs: list[str] = []
    errors: list[str] = []
    refresh_nodes: set[str] = set()

    def log(message: str) -> None:
        logs.append(message)
        if on_log is not None:
            on_log(message)

    def error(message: str) -> None:
        errors.append(message)
        log(message)

    cfg = load_config(config_path(root))
    from toolkit.core.infra.fleet_roles import FLEET_SERVICE_CATALOG
    from toolkit.services import get_service_plugin

    active_integrations = {*host.services, *host.applied_services}
    for integration in FLEET_SERVICE_CATALOG:
        if not integration.controller_lifecycle or integration.name not in active_integrations:
            continue
        plugin = get_service_plugin(integration.owner)
        if plugin is None:
            error(f"{integration.label}: integration owner is unavailable")
            continue
        try:
            for message in plugin.reconcile_host_integration(
                integration.name,
                cfg,
                host,
                root,
                selected=integration.name in host.services,
            ):
                log(message)
            refresh_nodes.update(
                node
                for node in plugin.host_integration_refresh_nodes(
                    integration.name,
                    cfg,
                    host,
                    selected=integration.name in host.services,
                )
                if node in cfg.enabled_nodes
            )
        except Exception as exc:
            error(f"{integration.label}: reconciliation failed for {host.name} ({exc})")
    try:
        from toolkit.core.ops.dns import sync_external_hosts_dns

        sync_external_hosts_dns(root, on_log=log)
    except (ValueError, RuntimeError, OSError) as exc:
        error(f"External host DNS: reconciliation failed ({exc})")
    return HostIntegrationReconcileResult(tuple(logs), tuple(errors), tuple(sorted(refresh_nodes)))


def cleanup_host_resources(
    root: Path,
    host: ExternalHost,
    *,
    on_log: Callable[[str], None] | None = None,
) -> list[str]:
    """Perform bounded best-effort teardown for a removed managed host."""
    logs: list[str] = []

    def log(message: str) -> None:
        logs.append(message)
        if on_log is not None:
            on_log(message)

    try:
        from toolkit.core.ops.dns import remove_external_host_dns

        remove_external_host_dns(root, host.name, on_log=log)
    except Exception as exc:
        log(f"External host DNS: cleanup skipped ({exc})")
    cfg = load_config(config_path(root))
    from toolkit.core.infra.fleet_roles import FLEET_SERVICE_CATALOG
    from toolkit.services import get_service_plugin

    active_integrations = {*host.services, *host.applied_services}
    for integration in FLEET_SERVICE_CATALOG:
        if not integration.controller_lifecycle or integration.name not in active_integrations:
            continue
        plugin = get_service_plugin(integration.owner)
        if plugin is None:
            log(f"{integration.label}: cleanup owner is unavailable")
            continue
        try:
            for message in plugin.cleanup_host_integration(integration.name, cfg, host, root):
                log(message)
        except Exception as exc:
            log(f"{integration.label}: cleanup skipped for {host.name} ({exc})")
    return logs


def host_integration_ansible_variables(root: Path, host: ExternalHost) -> dict[str, str]:
    """Collect selected integration variables from their owning plugins."""
    cfg = load_config(config_path(root))
    from toolkit.core.infra.fleet_roles import FLEET_SERVICE_CATALOG
    from toolkit.services import get_service_plugin

    variables: dict[str, str] = {}
    for integration in FLEET_SERVICE_CATALOG:
        if integration.name not in host.services:
            continue
        plugin = get_service_plugin(integration.owner)
        if plugin is None:
            raise RuntimeError(f"host integration owner {integration.owner!r} has no service plugin")
        owned = plugin.host_integration_ansible_variables(integration.name, cfg, host, root)
        collisions = set(variables).intersection(owned)
        if collisions:
            raise RuntimeError(f"host integration Ansible variable collision: {', '.join(sorted(collisions))}")
        variables.update(owned)
    return variables


def host_integration_statuses(
    root: Path,
    host: ExternalHost,
    *,
    probe: bool = True,
) -> tuple[HostIntegrationStatus, ...]:
    """Collect status for every selected integration without central service branches."""
    cfg = load_config(config_path(root))
    from toolkit.core.infra.fleet_roles import FLEET_SERVICE_CATALOG
    from toolkit.services import get_service_plugin

    statuses: list[HostIntegrationStatus] = []
    for integration in FLEET_SERVICE_CATALOG:
        if integration.name not in host.services:
            continue
        if not probe:
            statuses.append(HostIntegrationStatus(integration.name, integration.label, None, "SSH unavailable"))
            continue
        plugin = get_service_plugin(integration.owner)
        if plugin is None:
            statuses.append(HostIntegrationStatus(integration.name, integration.label, None, "plugin unavailable"))
            continue
        if not plugin.is_enabled(cfg):
            statuses.append(HostIntegrationStatus(integration.name, integration.label, None, "owner disabled"))
            continue
        try:
            result = plugin.host_integration_status(integration.name, cfg, host, root)
        except Exception as exc:
            statuses.append(
                HostIntegrationStatus(integration.name, integration.label, False, f"probe failed: {exc}"[:160])
            )
            continue
        if result is None:
            statuses.append(
                HostIntegrationStatus(integration.name, integration.label, None, "no runtime probe declared")
            )
            continue
        active, detail = result
        statuses.append(HostIntegrationStatus(integration.name, integration.label, active, detail[:160]))
    return tuple(statuses)


def mark_host_reconciled(root: Path, name: str, expected_fingerprint: str) -> bool:
    """Persist under the caller's fleet/controller operation lease without clobbering state."""
    with configuration_lock(root):
        cfg = load_config(config_path(root))
        current = next((host for host in cfg.external_hosts if host.name == name), None)
        if current is None or managed_host_fingerprint(current) != expected_fingerprint:
            return False
        stamp = datetime.now(UTC).replace(microsecond=0).isoformat()
        cfg.external_hosts = [
            host.model_copy(
                update={
                    "applied_services": list(host.services),
                    "reconciled": True,
                    "last_reconcile_at": stamp,
                }
            )
            if host.name == name
            else host
            for host in cfg.external_hosts
        ]
        save_config(cfg, config_path(root))
    return True


def remove_host(root: Path, name: str) -> bool:
    with configuration_mutation(root, "managed-host-remove"):
        cfg = load_config(config_path(root))
        removed = next((host for host in cfg.external_hosts if host.name == name), None)
        if removed is None:
            return False
        cfg.external_hosts = [host for host in cfg.external_hosts if host.name != name]
        save_config(with_host_integration_config(cfg, previous=removed, current=None), config_path(root))
    cleanup_host_resources(root, removed)
    return True


def list_hosts(root: Path) -> list[ExternalHost]:
    cfg = load_config(config_path(root))
    return cfg.external_hosts


def host_ssh_args(root: Path, host: ExternalHost) -> list[str] | None:
    from toolkit.core.ansible.ansible_ssh import resolve_ansible_ssh_key
    from toolkit.core.config.config import load_config

    cfg = load_config(config_path(root))
    key = resolve_ansible_ssh_key(cfg, root)
    if key is None:
        return None
    kh = root / "automation" / "ansible" / "inventory" / "known_hosts"
    args = [
        "ssh",
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "ConnectTimeout=15",
        "-p",
        str(host.ssh_port),
    ]
    if kh.is_file():
        args.extend(["-o", f"UserKnownHostsFile={kh}"])
    args.append(f"{host.ssh_user}@{host.ip}")
    return args


def test_host_connection(host: ExternalHost, *, root: Path) -> bool:
    """Test SSH connectivity to an external host (direct, no Proxmox jump)."""
    args = host_ssh_args(root, host)
    if args is None:
        return False
    args.append("echo homelab-ok")
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)
        return proc.returncode == 0 and "homelab-ok" in (proc.stdout or "")
    except (OSError, subprocess.TimeoutExpired):
        return False


def trust_host_key(host: ExternalHost, *, root: Path) -> list[str]:
    """Scan and store SSH host key for an external host."""
    kh = root / "automation" / "ansible" / "inventory" / "known_hosts"
    kh.parent.mkdir(parents=True, exist_ok=True)
    kh.touch(exist_ok=True)
    target = host.ip if host.ssh_port == 22 else f"[{host.ip}]:{host.ssh_port}"
    existing = subprocess.run(
        ["ssh-keygen", "-F", target, "-f", str(kh)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if existing.returncode == 0 and any(line and not line.startswith("#") for line in existing.stdout.splitlines()):
        return [f"SSH key already trusted for {host.name} ({host.ip})"]
    proc = subprocess.run(
        [
            "ssh-keyscan",
            "-p",
            str(host.ssh_port),
            "-H",
            host.ip,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return [f"ssh-keyscan failed for {host.ip}"]
    with kh.open("a") as fh:
        fh.write(proc.stdout)
        if not proc.stdout.endswith("\n"):
            fh.write("\n")
    return [f"Trusted SSH key for {host.name} ({host.ip})"]
