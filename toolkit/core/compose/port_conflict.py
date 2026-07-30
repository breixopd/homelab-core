"""Conflict detection for dynamically managed project containers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.infra.network_policy import declared_container_names, declared_service_ports
from toolkit.core.manifest.catalog import load_service_catalog

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def check_port_conflict(
    target_node: str,
    port: int,
    config: Config | None,
    *,
    root: Path | None = None,
) -> list[str]:
    """Return services and projects publishing ``port`` on the target node."""
    conflicts = [
        service
        for service, published, protocol in declared_service_ports(root or _repository_root()).get(target_node, [])
        if published == port and protocol == "tcp"
    ]
    if config is not None:
        from toolkit.core.projects.placement import project_node

        for entry in config.projects.entries:
            if entry.container_port == port and project_node(config, entry) == target_node:
                conflicts.append(entry.name or entry.subdomain)
    return list(dict.fromkeys(conflicts))


def check_container_name(name: str, config: Config | None, *, root: Path | None = None) -> str | None:
    """Return ``name`` when it collides with a service or existing project."""
    normalized = name.lower()
    repository_root = root or _repository_root()
    managed_names = set(load_service_catalog().names) | declared_container_names(repository_root)
    if normalized in managed_names:
        return name
    if config is not None:
        for entry in config.projects.entries:
            if normalized in {entry.name.lower(), entry.subdomain.lower()}:
                return name
    return None
