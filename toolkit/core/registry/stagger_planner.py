"""Compile health-gated startup waves from service and project manifests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


@dataclass(frozen=True, slots=True)
class StaggerWave:
    name: str
    services: tuple[str, ...]


def _compose_document(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read Compose model {path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
        raise ValueError(f"Compose model {path} must contain a services mapping")
    return document


def _active_services(document: dict[str, Any], profiles: frozenset[str]) -> dict[str, dict[str, Any]]:
    active: dict[str, dict[str, Any]] = {}
    for name, raw in document["services"].items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise ValueError("Compose services must be named mappings")
        declared_profiles = set(raw.get("profiles") or ())
        if not declared_profiles or declared_profiles.intersection(profiles):
            active[name] = raw
    return active


def _application_services(path: Path) -> tuple[str, ...]:
    return tuple(_compose_document(path)["services"])


def _service_dependencies(spec: dict[str, Any]) -> set[str]:
    dependencies = spec.get("depends_on") or ()
    if isinstance(dependencies, dict):
        result = set(dependencies)
    elif isinstance(dependencies, list):
        result = {value for value in dependencies if isinstance(value, str)}
    else:
        result = set()
    for field in ("network_mode", "ipc", "pid"):
        value = spec.get(field)
        if isinstance(value, str) and value.startswith("service:"):
            result.add(value.removeprefix("service:"))
    return result


def compose_stagger_waves(
    root: Path,
    cfg: Config,
    node: str,
    *,
    compose_path: Path,
    profiles: frozenset[str] | None = None,
) -> list[StaggerWave]:
    """Compile one ordered wave per active service or project plugin.

    Dependencies are resolved at plugin level so a multi-container application
    starts as one unit. Independent plugins are ordered deterministically by
    manifest priority and name.
    """
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_runtime_nodes
    from toolkit.core.manifest.routes import service_is_enabled
    from toolkit.core.projects.compose import project_service_name
    from toolkit.core.projects.placement import project_node

    active = _active_services(_compose_document(compose_path), profiles or frozenset())
    catalog = load_service_catalog(root)
    owners: dict[str, str] = {}
    priorities: dict[str, int] = {}
    manifest_dependencies: dict[str, set[str]] = {}

    for manifest in catalog.manifests:
        if not service_is_enabled(cfg, manifest):
            continue
        application_path = catalog.compose_path(manifest.name)
        if not application_path.is_file():
            continue
        application_services = _application_services(application_path)
        unknown = set(manifest.runtimes) - set(application_services)
        if unknown:
            rendered = ", ".join(sorted(unknown))
            raise ValueError(f"{manifest.name} places unknown runtime services: {rendered}")
        for service in application_services:
            placements = manifest_runtime_nodes(cfg, manifest, service)
            if node in placements and service in active:
                owners[service] = manifest.name
        priorities[manifest.name] = manifest.priority
        manifest_dependencies[manifest.name] = set(manifest.depends_on)

    for project in cfg.projects.entries:
        service = project_service_name(project)
        if project_node(cfg, project) == node and service in active:
            owners[service] = service
            priorities[service] = 10_000
            manifest_dependencies[service] = set()

    unowned = set(active) - set(owners)
    if unowned:
        raise ValueError(f"active Compose services have no plugin owner: {', '.join(sorted(unowned))}")

    grouped: dict[str, list[str]] = {}
    for service in active:
        grouped.setdefault(owners[service], []).append(service)

    dependencies: dict[str, set[str]] = {owner: set() for owner in grouped}
    for owner in grouped:
        for dependency in manifest_dependencies.get(owner, set()):
            if dependency in grouped and dependency != owner:
                dependencies[owner].add(dependency)
    for service, spec in active.items():
        owner = owners[service]
        for dependency in _service_dependencies(spec):
            dependency_owner = owners.get(dependency)
            if dependency_owner is not None and dependency_owner != owner:
                dependencies[owner].add(dependency_owner)

    remaining = set(grouped)
    completed: set[str] = set()
    waves: list[StaggerWave] = []
    while remaining:
        ready = [owner for owner in remaining if dependencies[owner].issubset(completed)]
        if not ready:
            blocked = "; ".join(
                f"{owner} -> {', '.join(sorted(dependencies[owner] - completed))}" for owner in sorted(remaining)
            )
            raise ValueError(f"service startup dependency cycle: {blocked}")
        owner = min(ready, key=lambda value: (priorities.get(value, 10_000), value))
        waves.append(StaggerWave(owner, tuple(grouped[owner])))
        completed.add(owner)
        remaining.remove(owner)
    return waves


def compose_dependency_map(root: Path) -> dict[str, list[str]]:
    """Return Compose and manifest dependencies for cascading recovery."""
    compose = root / "docker-compose.yml"
    merged: dict[str, list[str]] = {}
    if compose.is_file():
        from toolkit.core.registry.service_graph import ServiceGraph

        merged = {key: list(values) for key, values in ServiceGraph.from_compose(compose).dependency_map().items()}

    from toolkit.core.manifest.catalog import ManifestCatalogError, load_service_catalog

    try:
        catalog = load_service_catalog(root)
    except ManifestCatalogError:
        catalog = load_service_catalog()
    for manifest in catalog.manifests:
        bucket = merged.setdefault(manifest.name, [])
        for dependency in manifest.depends_on:
            if dependency not in bucket:
                bucket.append(dependency)
    return merged
