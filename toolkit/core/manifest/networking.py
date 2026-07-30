"""Compile plugin-owned host listener contracts into concrete network endpoints."""

from __future__ import annotations

from dataclasses import dataclass

from toolkit.core.config.config import Config
from toolkit.core.manifest.catalog import ServiceCatalog, load_service_catalog


@dataclass(frozen=True, slots=True)
class CompiledNetworkListener:
    service: str
    listener_id: str
    target_nodes: tuple[str, ...]
    sources: tuple[str, ...]
    port: int
    protocol: str


def compile_network_listeners(
    cfg: Config,
    catalog: ServiceCatalog | None = None,
) -> tuple[CompiledNetworkListener, ...]:
    """Resolve enabled listener manifests against placement and network policy."""
    from toolkit.core.manifest.placement import manifest_node, manifest_runtime_nodes, resolve_node_selector
    from toolkit.core.manifest.routes import predicate_matches, service_is_enabled
    from toolkit.core.registry.mesh import mesh_lan_cidr

    selected = catalog or load_service_catalog()
    lan_cidr = mesh_lan_cidr(cfg)
    mesh_cidr = cfg.network.mesh_ipv4_cidr
    compiled: list[CompiledNetworkListener] = []

    for manifest in selected.manifests:
        if not service_is_enabled(cfg, manifest, selected):
            continue
        for listener in manifest.network_listeners:
            if not all(predicate_matches(cfg, predicate, selected) for predicate in listener.enabled_when):
                continue

            sources: list[str] = []
            for selector in listener.sources:
                if selector == "@all":
                    sources.extend(cfg.node_ip(node) for node in cfg.enabled_nodes)
                elif selector == "@internet":
                    if cfg.network.expose_via_internet:
                        sources.append("any")
                elif selector == "@lan":
                    sources.append(lan_cidr)
                elif selector == "@mesh":
                    sources.append(mesh_cidr)
                elif selector.startswith("@runtime:"):
                    runtime = selector.removeprefix("@runtime:")
                    sources.extend(cfg.node_ip(node) for node in manifest_runtime_nodes(cfg, manifest, runtime))
                elif selector.startswith("@service:"):
                    service = selected.require(selector.removeprefix("@service:"))
                    if service_is_enabled(cfg, service, selected):
                        sources.append(cfg.node_ip(manifest_node(cfg, service)))
                elif selector.startswith("@integration:"):
                    integration = selector.removeprefix("@integration:")
                    if any(host.kind == "fleet" and integration in host.services for host in cfg.external_hosts):
                        sources.append(mesh_cidr)
                    sources.extend(
                        host.ip for host in cfg.external_hosts if host.kind == "plain" and integration in host.services
                    )
                else:
                    sources.append(cfg.node_ip(resolve_node_selector(cfg, selector)))

            resolved_sources = tuple(dict.fromkeys(sources))
            if not resolved_sources:
                continue
            target_nodes = (
                (manifest_node(cfg, manifest),)
                if listener.host_process or not listener.runtime_service
                else manifest_runtime_nodes(cfg, manifest, listener.runtime_service)
            )
            compiled.append(
                CompiledNetworkListener(
                    service=manifest.name,
                    listener_id=listener.id,
                    target_nodes=target_nodes,
                    sources=resolved_sources,
                    port=listener.port,
                    protocol=listener.protocol,
                )
            )
    return tuple(compiled)
