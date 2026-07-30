"""Revisioned desired-state resources for managed external hosts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from toolkit.controller.desired_state_api import DesiredStateConflictError, DesiredStateValidationError
from toolkit.controller.read_models import (
    ManagedHostCreate,
    ManagedHostIntegrationFieldChoice,
    ManagedHostServiceChoice,
    ManagedHostSpec,
    ManagedHostsView,
    ManagedHostUpdate,
    ManagedHostView,
)
from toolkit.core.config.config import Config, ExternalHost, load_config, save_config
from toolkit.core.config.mutations import config_revision, configuration_lock, configuration_mutation
from toolkit.core.config.storage import config_path
from toolkit.core.infra.fleet_roles import FLEET_SERVICE_CATALOG
from toolkit.core.infra.hosts import cleanup_host_resources, managed_host_fingerprint, with_host_integration_config

if TYPE_CHECKING:
    from toolkit.core.deploy.operation_lease import OperationLease


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _host_view(host: ExternalHost) -> ManagedHostView:
    return ManagedHostView(
        fingerprint=managed_host_fingerprint(host),
        name=host.name,
        ip=host.ip,
        kind=host.kind,
        ssh_user=host.ssh_user,
        ssh_port=host.ssh_port,
        cluster_group=host.cluster_group,
        lldap_email=host.lldap_email,
        headscale_tags=list(host.headscale_tags),
        services=list(host.services),
        applied_services=list(host.applied_services),
        integrations=host.integrations,
        reconciled=host.reconciled,
        last_reconcile_at=_timestamp(host.last_reconcile_at),
    )


def _service_choices() -> list[ManagedHostServiceChoice]:
    return [
        ManagedHostServiceChoice(
            name=service.name,
            label=service.label,
            default_for_plain=service.is_default_for("plain"),
            default_for_fleet=service.is_default_for("fleet"),
            fleet_only=service.kinds == ("fleet",),
            fields=[
                ManagedHostIntegrationFieldChoice(
                    key=field.key,
                    label=field.label,
                    description=field.description,
                    type=field.type,
                    required=field.required,
                    default=field.default,
                    placeholder=field.placeholder,
                )
                for field in service.fields
            ],
        )
        for service in FLEET_SERVICE_CATALOG
    ]


def parse_managed_host_integrations(
    services: list[str],
    value_for: Callable[[str], object | None],
) -> dict[str, dict[str, bool | int | float | str]]:
    """Parse manifest-owned host fields at the controller boundary."""
    from toolkit.core.infra.fleet_roles import parse_host_integration_value

    choices = {choice.name: choice for choice in FLEET_SERVICE_CATALOG}
    integrations: dict[str, dict[str, bool | int | float | str]] = {}
    for service_name in services:
        choice = choices.get(service_name)
        if choice is None:
            continue
        for field in choice.fields:
            form_key = f"integration.{service_name}.{field.key}"
            raw_value = value_for(form_key)
            raw = str(raw_value or ("false" if field.type == "boolean" else ""))
            if not raw.strip() and field.default is None:
                continue
            integrations.setdefault(service_name, {})[field.key] = parse_host_integration_value(field, raw)
    return integrations


def _view(cfg: Config, revision: str) -> ManagedHostsView:
    return ManagedHostsView(
        revision=revision,
        hosts=[_host_view(host) for host in sorted(cfg.external_hosts, key=lambda item: item.name.lower())[:128]],
        service_choices=_service_choices(),
    )


def read_managed_hosts_view(root: Path) -> ManagedHostsView:
    root = root.resolve()
    with configuration_lock(root):
        return _view(load_config(config_path(root)), config_revision(root))


def _external_host(spec: ManagedHostSpec, existing: ExternalHost | None = None) -> ExternalHost:
    try:
        candidate = ExternalHost.model_validate(spec.model_dump(mode="python"))
    except (ValidationError, ValueError) as exc:
        raise DesiredStateValidationError("managed host is invalid") from exc
    if existing is None:
        return candidate
    desired_fields = set(type(candidate).model_fields) - {"reconciled", "last_reconcile_at"}
    unchanged = all(getattr(candidate, field) == getattr(existing, field) for field in desired_fields)
    if unchanged:
        return candidate.model_copy(
            update={
                "applied_services": list(existing.applied_services),
                "reconciled": existing.reconciled,
                "last_reconcile_at": existing.last_reconcile_at,
            }
        )
    return candidate.model_copy(update={"applied_services": list(existing.applied_services)})


def create_managed_host(root: Path, request: ManagedHostCreate) -> ManagedHostsView:
    root = root.resolve()
    with configuration_mutation(root, "managed-host:create"):
        if config_revision(root) != request.expected_revision:
            raise DesiredStateConflictError("configuration changed")
        cfg = load_config(config_path(root))
        if any(host.name == request.host.name for host in cfg.external_hosts):
            raise DesiredStateConflictError("managed host already exists")
        host = _external_host(request.host)
        cfg.external_hosts.append(host)
        updated = with_host_integration_config(cfg, previous=None, current=host)
        save_config(Config.model_validate(updated.model_dump(mode="python")), config_path(root))
    return read_managed_hosts_view(root)


def update_managed_host(root: Path, name: str, request: ManagedHostUpdate) -> ManagedHostsView:
    root = root.resolve()
    if request.host.name != name:
        raise DesiredStateValidationError("managed host identity cannot be changed")
    with configuration_mutation(root, "managed-host:update"):
        if config_revision(root) != request.expected_revision:
            raise DesiredStateConflictError("configuration changed")
        cfg = load_config(config_path(root))
        previous = next((host for host in cfg.external_hosts if host.name == name), None)
        if previous is None:
            raise DesiredStateConflictError("managed host does not exist")
        host = _external_host(request.host, previous)
        cfg.external_hosts = [host if item.name == name else item for item in cfg.external_hosts]
        updated = with_host_integration_config(cfg, previous=previous, current=host)
        save_config(Config.model_validate(updated.model_dump(mode="python")), config_path(root))
    return read_managed_hosts_view(root)


def remove_managed_host(
    root: Path,
    name: str,
    expected_fingerprint: str,
    *,
    on_log: Callable[[str], None] | None = None,
    operation_lease: OperationLease | None = None,
) -> list[str]:
    """Clean integrations, then atomically remove an unchanged host record."""
    root = root.resolve()
    if operation_lease is not None:
        operation_lease.assert_owns_root(root)
        operation_lease.raise_if_cancelled()
    boundary = configuration_lock(root) if operation_lease else configuration_mutation(root, "managed-host:remove")
    with boundary:
        cfg = load_config(config_path(root))
        snapshot = next((host for host in cfg.external_hosts if host.name == name), None)
        if snapshot is None:
            raise DesiredStateConflictError("managed host does not exist")
        if managed_host_fingerprint(snapshot) != expected_fingerprint:
            raise DesiredStateConflictError("managed host changed")

        logs = cleanup_host_resources(root, snapshot, on_log=on_log)
        cfg = load_config(config_path(root))
        current = next((host for host in cfg.external_hosts if host.name == name), None)
        if current is None or managed_host_fingerprint(current) != expected_fingerprint:
            raise DesiredStateConflictError("managed host changed during cleanup")
        cfg.external_hosts = [host for host in cfg.external_hosts if host.name != name]
        updated = with_host_integration_config(cfg, previous=current, current=None)
        save_config(Config.model_validate(updated.model_dump(mode="python")), config_path(root))
    return logs
