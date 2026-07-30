"""Resolve the enabled database provider that owns recovery operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.schema import ServiceManifest


@dataclass(frozen=True, slots=True)
class PrimaryDatabaseProvider:
    """One enabled service plugin that owns primary database recovery."""

    manifest: ServiceManifest
    plugin: ServicePlugin


_MAINTENANCE_METHODS = (
    "pre_deploy_database_dump",
    "list_database_dumps",
    "restore_database_dump",
    "run_database_restore_drill",
)


class DatabaseProviderDisabledError(RuntimeError):
    """The catalog has a primary database provider, but desired state disables it."""


def primary_database_provider(cfg: Config) -> PrimaryDatabaseProvider:
    """Return the enabled plugin selected by the ``primary-database`` capability.

    Recovery flows fail closed when a provider is disabled or its plugin omits
    part of the maintenance contract. This prevents deployments from silently
    proceeding without a configured recovery safety gate.
    """
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import service_is_enabled
    from toolkit.services import get_service_plugin

    catalog = load_service_catalog()
    manifest = catalog.require_provider("primary-database")
    if manifest.database_provider is None:
        raise RuntimeError(f"primary database provider {manifest.name!r} has no database_provider manifest contract")
    if not service_is_enabled(cfg, manifest, catalog):
        raise DatabaseProviderDisabledError(f"primary database provider {manifest.name!r} is disabled")
    plugin = get_service_plugin(manifest.name)
    if plugin is None:
        raise RuntimeError(f"primary database provider {manifest.name!r} has no service plugin")
    missing = [
        method for method in _MAINTENANCE_METHODS if getattr(type(plugin), method) is getattr(ServicePlugin, method)
    ]
    if missing:
        raise RuntimeError(f"primary database provider {manifest.name!r} lacks maintenance hooks: {', '.join(missing)}")
    return PrimaryDatabaseProvider(manifest, plugin)


def primary_database_node(cfg: Config, provider: PrimaryDatabaseProvider, override: str | None = None) -> str:
    """Resolve the primary provider's configured node or validate an override."""
    if override is not None:
        cfg.node_ip(override)
        return override
    from toolkit.core.manifest.placement import manifest_node

    return manifest_node(cfg, provider.manifest)
