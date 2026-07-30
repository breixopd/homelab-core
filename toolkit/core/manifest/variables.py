"""Compile bounded service-manifest variables from typed configuration."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from toolkit.core.config.config import Config
from toolkit.core.manifest.schema import ServiceManifest

if TYPE_CHECKING:
    from toolkit.core.manifest.catalog import ServiceCatalog

_CONFIG_VARIABLE = re.compile(r"\{config\.([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)\}")
_SETTING_VARIABLE = re.compile(r"\{setting\.([a-z][a-z0-9-]{0,62})\}")
_SERVICE_VARIABLE = re.compile(r"\{service\.([a-z0-9][a-z0-9-]{0,62})\.(address|node)\}")
_DERIVED_VARIABLE = re.compile(r"\{derived\.(public_url_protocol|ldap_base_dn|admin_email|edge_proxy_cidr)\}")


def _scalar_text(value: bool | int | float | str) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def domain_to_base_dn(domain: str) -> str:
    """Convert a DNS domain into an LDAP base DN."""
    if not domain or domain == "localhost":
        return "dc=home,dc=local"
    return ",".join(f"dc={part}" for part in domain.split("."))


def _derived_value(
    cfg: Config,
    name: str,
    service_nodes: Mapping[str, str] | None = None,
    capability_providers: Mapping[str, str] | None = None,
) -> str:
    if name == "public_url_protocol":
        return "http" if cfg.domain == "localhost" else "https"
    if name == "ldap_base_dn":
        return domain_to_base_dn(cfg.domain)
    if name == "admin_email":
        return cfg.email or f"admin@{cfg.domain}"
    if name == "edge_proxy_cidr":
        if not cfg.is_multi_node:
            from toolkit.core.infra.edge_network import edge_network_values

            return f"{edge_network_values(cfg)[1]}/32"
        if capability_providers is None:
            from toolkit.core.manifest.catalog import load_service_catalog

            ingress = load_service_catalog().require_provider("ingress").name
        else:
            try:
                ingress = capability_providers["ingress"]
            except KeyError as exc:
                raise ValueError("edge proxy CIDR requires an ingress capability provider") from exc
        if service_nodes is not None:
            try:
                node = service_nodes[ingress]
            except KeyError as exc:
                raise ValueError(f"ingress provider {ingress!r} has no placement") from exc
            return f"{cfg.machines[node].address}/32"
        from toolkit.core.manifest.placement import service_address

        return f"{service_address(cfg, ingress)}/32"
    raise ValueError(f"unknown derived manifest variable {name!r}")


def _config_value(cfg: Config, path: str) -> str:
    value: object = cfg
    for part in path.split("."):
        if isinstance(value, BaseModel) and part in type(value).model_fields:
            value = getattr(value, part)
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise ValueError(f"manifest variable references unknown config path {path!r}")
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str | int | float):
        return str(value)
    raise ValueError(f"manifest variable config path {path!r} must resolve to a scalar")


def compile_manifest_variables(
    cfg: Config,
    manifest: ServiceManifest,
    *,
    service_nodes: Mapping[str, str] | None = None,
    capability_providers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve one manifest's validated variable templates."""
    from toolkit.core.manifest.placement import service_address, service_node
    from toolkit.core.manifest.settings import service_setting_value

    def replace_service(match: re.Match[str]) -> str:
        service, attribute = match.groups()
        if service_nodes is not None:
            try:
                node = cfg.control_node if not cfg.is_multi_node else service_nodes[service]
            except KeyError as exc:
                raise ValueError(f"manifest variable references unknown service {service!r}") from exc
            try:
                machine = cfg.machines[node]
            except KeyError as exc:
                raise ValueError(f"service {service!r} targets unknown machine {node!r}") from exc
            return machine.address if attribute == "address" else node
        return service_address(cfg, service) if attribute == "address" else service_node(cfg, service)

    compiled: dict[str, str] = {}
    for name, value in manifest.variables.items():
        config_resolved = _CONFIG_VARIABLE.sub(lambda match: _config_value(cfg, match.group(1)), value)
        setting_resolved = _SETTING_VARIABLE.sub(
            lambda match: _scalar_text(service_setting_value(cfg, manifest, match.group(1))),
            config_resolved,
        )
        service_resolved = _SERVICE_VARIABLE.sub(replace_service, setting_resolved)
        compiled[name] = _DERIVED_VARIABLE.sub(
            lambda match: _derived_value(
                cfg,
                match.group(1),
                service_nodes,
                capability_providers,
            ),
            service_resolved,
        )
    return compiled


def compile_manifest_host_sources(
    cfg: Config,
    manifest: ServiceManifest,
    install_root: str | Path,
    *,
    catalog: ServiceCatalog | None = None,
) -> dict[str, str]:
    """Resolve one service's typed host bind sources below the install root."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import predicate_matches

    selected = catalog or load_service_catalog()
    root = Path(install_root)
    compiled: dict[str, str] = {}
    for name, source in manifest.host_sources.items():
        matches = [variant for variant in source.variants if predicate_matches(cfg, variant.when, selected)]
        if len(matches) > 1:
            raise ValueError(f"host source {manifest.name}.{name} matched multiple path variants")
        relative = matches[0].path if matches else source.path
        compiled[name] = str(root / relative)
    return compiled


def _service_endpoint_address(
    cfg: Config,
    provider: ServiceManifest,
    *,
    service_nodes: Mapping[str, str],
    consumer_node: str,
) -> tuple[str, int]:
    contract = provider.service_endpoint
    if contract is None:
        raise ValueError(f"service {provider.name!r} does not declare a service endpoint")
    provider_node = cfg.control_node if not cfg.is_multi_node else service_nodes[provider.name]
    if consumer_node == provider_node:
        return contract.compose_service or provider.name, contract.container_port
    if contract.published_port is None:
        raise ValueError(f"service {provider.name!r} requires published_port for a cross-node integration")
    return cfg.machines[provider_node].address, contract.published_port


def compile_manifest_integration_variables(
    cfg: Config,
    manifest: ServiceManifest,
    *,
    catalog: ServiceCatalog | None = None,
    service_nodes: Mapping[str, str] | None = None,
    consumer_node: str | None = None,
) -> dict[str, str]:
    """Resolve one service's required and optional integration environments."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import service_node_map

    selected = catalog or load_service_catalog()
    nodes = dict(service_nodes or service_node_map(cfg, selected))
    resolved_consumer = consumer_node or (cfg.control_node if not cfg.is_multi_node else nodes[manifest.name])
    compiled: dict[str, str] = {}
    from toolkit.core.manifest.routes import service_is_enabled

    for integration in manifest.integrations:
        provider = selected.require(integration.service)
        enabled = service_is_enabled(cfg, provider, selected)
        if integration.required and not enabled:
            raise ValueError(f"required integration {manifest.name!r} -> {provider.name!r} is disabled")
        if integration.enabled_env:
            compiled[integration.enabled_env] = str(enabled).lower()
        if not enabled:
            for name in (integration.host_env, integration.port_env, integration.address_env, integration.url_env):
                if name:
                    compiled[name] = ""
            continue
        host, port = _service_endpoint_address(
            cfg,
            provider,
            service_nodes=nodes,
            consumer_node=resolved_consumer,
        )
        if integration.host_env:
            compiled[integration.host_env] = host
        if integration.port_env:
            compiled[integration.port_env] = str(port)
        if integration.address_env:
            compiled[integration.address_env] = f"{host}:{port}"
        if integration.url_env:
            compiled[integration.url_env] = f"{integration.scheme}://{host}:{port}"
    return compiled


def compile_role_host_sources(cfg: Config, role: str, install_root: str | Path) -> dict[str, str]:
    """Resolve enabled host sources required by one machine role."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_node, manifest_runtime_nodes
    from toolkit.core.manifest.routes import service_is_enabled

    catalog = load_service_catalog()
    compiled: dict[str, str] = {}
    owners: dict[str, str] = {}
    for manifest in catalog.manifests:
        if not service_is_enabled(cfg, manifest, catalog):
            continue
        roles = {manifest_node(cfg, manifest)}
        for runtime_service in manifest.runtimes:
            roles.update(manifest_runtime_nodes(cfg, manifest, runtime_service))
        if cfg.is_multi_node and role not in roles:
            continue
        for name, value in compile_manifest_host_sources(cfg, manifest, install_root, catalog=catalog).items():
            if name in compiled and compiled[name] != value:
                raise ValueError(f"conflicting host source {name!r}: {owners[name]!r} and {manifest.name!r}")
            compiled[name] = value
            owners[name] = manifest.name
    return compiled


def compile_role_variables(cfg: Config, role: str) -> dict[str, str]:
    """Resolve enabled application variables assigned to one node role."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_node, manifest_runtime_nodes, service_node_map
    from toolkit.core.manifest.routes import service_is_enabled

    catalog = load_service_catalog()
    service_nodes = service_node_map(cfg, catalog)
    capability_providers = {
        capability: manifest.name for manifest in catalog.manifests for capability in manifest.provides
    }
    compiled: dict[str, str] = {}
    owners: dict[str, str] = {}

    def add(name: str, value: str, owner: str) -> None:
        if name in compiled and compiled[name] != value:
            raise ValueError(f"conflicting manifest variable {name!r}: {owners[name]!r} and {owner!r}")
        compiled[name] = value
        owners[name] = owner

    for manifest in catalog.manifests:
        if not service_is_enabled(cfg, manifest):
            continue
        roles = {manifest_node(cfg, manifest)}
        for runtime_service in manifest.runtimes:
            roles.update(manifest_runtime_nodes(cfg, manifest, runtime_service))
        if cfg.is_multi_node and role not in roles:
            continue
        for name, value in compile_manifest_variables(
            cfg,
            manifest,
            service_nodes=service_nodes,
            capability_providers=capability_providers,
        ).items():
            add(name, value, manifest.name)
        for name, value in compile_manifest_integration_variables(
            cfg,
            manifest,
            catalog=catalog,
            service_nodes=service_nodes,
            consumer_node=role,
        ).items():
            add(name, value, manifest.name)
        consumer_node = cfg.control_node if not cfg.is_multi_node else service_nodes[manifest.name]
        if role != consumer_node:
            continue
        for binding in manifest.databases:
            provider = catalog.require(binding.provider)
            contract = provider.database_provider
            if contract is None:
                raise ValueError(f"service {binding.provider!r} does not declare a database provider")
            host, port = _service_endpoint_address(
                cfg,
                provider,
                service_nodes=service_nodes,
                consumer_node=consumer_node,
            )
            add(binding.host_env, host, manifest.name)
            add(binding.port_env, str(port), manifest.name)
            add(binding.database_env, binding.database, manifest.name)
            add(binding.username_env, binding.username, manifest.name)
    return compiled


def compile_role_secret_projections(
    cfg: Config,
    role: str,
    secrets: Mapping[str, str],
    *,
    catalog: ServiceCatalog | None = None,
) -> dict[str, str]:
    """Project stored secrets into service-owned runtime names on one node."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_node, manifest_runtime_nodes
    from toolkit.core.manifest.routes import service_is_enabled

    selected = catalog or load_service_catalog()
    compiled: dict[str, str] = {}
    owners: dict[str, str] = {}
    for manifest in selected.manifests:
        if not service_is_enabled(cfg, manifest, selected):
            continue
        roles = {manifest_node(cfg, manifest)}
        for runtime_service in manifest.runtimes:
            roles.update(manifest_runtime_nodes(cfg, manifest, runtime_service))
        if cfg.is_multi_node and role not in roles:
            continue
        for projection in manifest.secret_projections:
            previous = owners.get(projection.target_env)
            if previous is not None:
                raise ValueError(
                    f"runtime secret {projection.target_env!r} is projected by both {previous!r} and {manifest.name!r}"
                )
            owners[projection.target_env] = manifest.name
            compiled[projection.target_env] = secrets.get(projection.source_env, "")
    return compiled


def compile_role_secret_fallbacks(
    cfg: Config,
    role: str,
    secrets: Mapping[str, str],
    *,
    catalog: ServiceCatalog | None = None,
) -> dict[str, str]:
    """Resolve service-owned fallback secrets without exposing their sources."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_node, manifest_runtime_nodes
    from toolkit.core.manifest.routes import service_is_enabled

    selected = catalog or load_service_catalog()
    compiled: dict[str, str] = {}
    for manifest in selected.manifests:
        if not service_is_enabled(cfg, manifest, selected):
            continue
        roles = {manifest_node(cfg, manifest)}
        for runtime_service in manifest.runtimes:
            roles.update(manifest_runtime_nodes(cfg, manifest, runtime_service))
        if cfg.is_multi_node and role not in roles:
            continue
        for secret in manifest.required_secrets:
            if secret.fallback_env is None:
                continue
            value = (secrets.get(secret.name) or secrets.get(secret.fallback_env) or "").strip()
            if value:
                compiled[secret.name] = value
    return compiled
