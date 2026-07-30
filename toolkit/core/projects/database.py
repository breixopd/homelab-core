"""Manifest-driven database providers for managed projects."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from toolkit.core.config.config import Config, ProjectEntry
from toolkit.core.projects.secrets import project_database_secret_name

if TYPE_CHECKING:
    from toolkit.core.manifest.schema import ServiceManifest


def sanitize_postgres_identifier(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]", "_", value.lower())
    if cleaned and cleaned[0].isdigit():
        cleaned = f"prj_{cleaned}"
    return cleaned[:63] or "prj"


def project_postgres_user(entry: ProjectEntry) -> str:
    return sanitize_postgres_identifier(entry.subdomain)


def project_postgres_database(entry: ProjectEntry) -> str:
    return sanitize_postgres_identifier(entry.subdomain)


def project_database_env_pairs(cfg: Config, database_service: str) -> list[tuple[str, str]]:
    return [
        (project_postgres_user(entry), project_database_secret_name(entry.subdomain))
        for entry in cfg.projects.entries
        if entry.database_service == database_service
    ]


def project_database_providers(config: Config) -> tuple[ServiceManifest, ...]:
    """Return enabled services that declare the project-database contract."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import service_is_enabled

    catalog = load_service_catalog()
    return tuple(
        manifest
        for manifest in catalog.manifests
        if manifest.database_provider is not None and service_is_enabled(config, manifest, catalog)
    )


def project_database_provider(config: Config, project: ProjectEntry) -> ServiceManifest | None:
    """Resolve one project's validated database provider."""
    if not project.database_service:
        return None
    from toolkit.core.manifest.catalog import load_service_catalog

    return load_service_catalog().require(project.database_service)


def project_database_nodes(config: Config, project: ProjectEntry) -> tuple[str, ...]:
    """Return the nodes that need a project's generated database secret."""
    provider = project_database_provider(config, project)
    if provider is None:
        return ()
    from toolkit.core.manifest.placement import manifest_node
    from toolkit.core.projects.placement import project_node

    return tuple(dict.fromkeys((project_node(config, project), manifest_node(config, provider))))
