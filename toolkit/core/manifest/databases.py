"""Compile service-owned database requests against provider contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.catalog import ServiceCatalog


@dataclass(frozen=True, slots=True)
class CompiledDatabaseBinding:
    service: str
    provider: str
    engine: Literal["postgresql"]
    database: str
    username: str
    host_env: str
    port_env: str
    database_env: str
    username_env: str
    password_env: str


def compile_database_bindings(
    cfg: Config,
    catalog: ServiceCatalog | None = None,
    *,
    provider: str | None = None,
) -> tuple[CompiledDatabaseBinding, ...]:
    """Return enabled consumer bindings resolved against enabled providers."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import service_is_enabled

    selected = catalog or load_service_catalog()
    enabled = {
        manifest.name: manifest for manifest in selected.manifests if service_is_enabled(cfg, manifest, selected)
    }
    compiled: list[CompiledDatabaseBinding] = []
    for manifest in selected.manifests:
        if manifest.name not in enabled:
            continue
        for binding in manifest.databases:
            if provider is not None and binding.provider != provider:
                continue
            provider_manifest = enabled.get(binding.provider)
            if provider_manifest is None or provider_manifest.database_provider is None:
                continue
            compiled.append(
                CompiledDatabaseBinding(
                    service=manifest.name,
                    provider=binding.provider,
                    engine=provider_manifest.database_provider.engine,
                    database=binding.database,
                    username=binding.username,
                    host_env=binding.host_env,
                    port_env=binding.port_env,
                    database_env=binding.database_env,
                    username_env=binding.username_env,
                    password_env=binding.password_env,
                )
            )
    return tuple(compiled)
