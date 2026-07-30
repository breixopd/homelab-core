"""Manifest-derived host integration catalog shared by CLI, UI, and Ansible."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from toolkit.core.manifest.catalog import load_service_catalog

HostKind = Literal["plain", "fleet"]


@dataclass(frozen=True, slots=True)
class HostIntegrationField:
    key: str
    label: str
    description: str
    type: Literal["boolean", "integer", "number", "path", "text"]
    required: bool
    default: bool | int | float | str | None
    placeholder: str


@dataclass(frozen=True, slots=True)
class HostService:
    name: str
    label: str
    owner: str
    kinds: tuple[HostKind, ...]
    default_for: tuple[HostKind, ...]
    after: tuple[str, ...] = ()
    ansible_role: str = ""
    controller_lifecycle: bool = False
    fields: tuple[HostIntegrationField, ...] = ()

    def is_selectable_for(self, kind: HostKind) -> bool:
        return kind in self.kinds

    def is_default_for(self, kind: HostKind) -> bool:
        return kind in self.default_for


def _load_host_services() -> tuple[HostService, ...]:
    services = tuple(
        HostService(
            name=integration.id,
            label=integration.label,
            owner=manifest.name,
            kinds=integration.kinds,
            default_for=integration.default_for,
            after=integration.after,
            ansible_role=integration.ansible_role,
            controller_lifecycle=integration.controller_lifecycle,
            fields=tuple(
                HostIntegrationField(
                    key=field.key,
                    label=field.label,
                    description=field.description,
                    type=field.type,
                    required=field.required,
                    default=field.default,
                    placeholder=field.placeholder,
                )
                for field in integration.fields
            ),
        )
        for manifest in load_service_catalog().manifests
        for integration in manifest.host_integrations
    )
    names = [service.name for service in services]
    if len(names) != len(set(names)):
        raise RuntimeError("host integration identifiers must be globally unique")
    by_name = {service.name: service for service in services}
    ordered: list[HostService] = []
    pending = list(services)
    while pending:
        completed = {item.name for item in ordered}
        ready = [service for service in pending if all(dependency in completed for dependency in service.after)]
        if not ready:
            blocked = ", ".join(service.name for service in pending)
            raise RuntimeError(f"host integration ordering is cyclic or incomplete: {blocked}")
        for service in ready:
            ordered.append(service)
            pending.remove(service)
    if set(by_name) != {service.name for service in ordered}:
        raise RuntimeError("host integration ordering lost catalog entries")
    return tuple(ordered)


FLEET_SERVICE_CATALOG: tuple[HostService, ...] = _load_host_services()
LDAP_CLIENT_SERVICE = "ldap-client"


def services_for_kind(kind: HostKind, *, defaults_only: bool = False) -> list[str]:
    return [
        service.name
        for service in FLEET_SERVICE_CATALOG
        if service.is_selectable_for(kind) and (not defaults_only or service.is_default_for(kind))
    ]


FLEET_SELECTABLE_SERVICES: tuple[str, ...] = tuple(services_for_kind("fleet"))
FLEET_BASELINE_SERVICES: tuple[str, ...] = tuple(services_for_kind("fleet", defaults_only=True))


def fleet_default_services() -> list[str]:
    return services_for_kind("fleet", defaults_only=True)


def plain_host_default_services() -> list[str]:
    return services_for_kind("plain", defaults_only=True)


def external_host_default_services() -> list[str]:
    return plain_host_default_services()


def parse_host_integration_value(field: HostIntegrationField, raw: str) -> bool | int | float | str:
    """Parse a CLI or form string according to a manifest-declared field."""
    value = raw.strip()
    if field.type == "boolean":
        normalized = value.lower()
        if normalized not in {"true", "false"}:
            raise ValueError(f"{field.key} must be true or false")
        return normalized == "true"
    if field.type == "integer":
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"{field.key} must be an integer") from exc
    if field.type == "number":
        try:
            return float(value)
        except ValueError as exc:
            raise ValueError(f"{field.key} must be a number") from exc
    return value


def parse_host_integration_assignments(
    services: list[str], assignments: tuple[str, ...] | list[str]
) -> dict[str, dict[str, bool | int | float | str]]:
    """Parse repeated ``service.field=value`` assignments against the catalog."""
    catalog = {service.name: service for service in FLEET_SERVICE_CATALOG}
    parsed: dict[str, dict[str, bool | int | float | str]] = {}
    for assignment in assignments:
        target, separator, raw = assignment.partition("=")
        integration, dot, key = target.partition(".")
        if not separator or not dot or not integration or not key:
            raise ValueError(f"invalid integration setting {assignment!r}; expected service.field=value")
        if integration not in services:
            raise ValueError(f"integration setting {target!r} belongs to an unselected service")
        service = catalog.get(integration)
        if service is None:
            raise ValueError(f"unknown host integration {integration!r}")
        fields = {field.key: field for field in service.fields}
        field = fields.get(key)
        if field is None:
            raise ValueError(f"unknown {integration} integration field {key!r}")
        parsed.setdefault(integration, {})[key] = parse_host_integration_value(field, raw)
    return parsed


def ansible_roles_for_services(services: list[str], *, kind: HostKind, cfg=None) -> list[str]:
    """Return roles for selected integrations, omitting disabled service owners."""
    catalog = {service.name: service for service in FLEET_SERVICE_CATALOG}
    selected_names = list(dict.fromkeys(services))
    selected = set(selected_names)
    ordered: list[HostService] = []
    pending = [catalog[name] for name in selected_names if name in catalog]
    while pending:
        completed = {service.name for service in ordered}
        ready = [
            service
            for service in pending
            if all(dependency not in selected or dependency in completed for dependency in service.after)
        ]
        if not ready:
            raise RuntimeError("selected host integrations have an ordering cycle")
        for service in ready:
            ordered.append(service)
            pending.remove(service)

    roles: list[str] = []
    for service in ordered:
        if service is not None and cfg is not None:
            from toolkit.services import get_service_plugin

            owner = get_service_plugin(service.owner)
            if owner is None or not owner.is_enabled(cfg):
                continue
        if (
            service is not None
            and service.is_selectable_for(kind)
            and service.ansible_role
            and service.ansible_role not in roles
        ):
            roles.append(service.ansible_role)
    return roles
