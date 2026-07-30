"""Generate auditable guest ingress rules from role and service declarations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import yaml

from toolkit.core.compose.ports import compose_published_ports

if TYPE_CHECKING:
    from toolkit.core.manifest.catalog import ServiceCatalog


class FirewallRule(TypedDict):
    roles: list[str]
    from_ip: str
    port: int
    proto: str
    comment: str


def _manifest_nodes(services_dir: Path, cfg=None) -> dict[str, str]:
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.placement import resolve_node_selector

    config = cfg or Config()
    roles: dict[str, str] = {}
    if not services_dir.is_dir():
        return roles
    for service_dir in sorted(path for path in services_dir.iterdir() if path.is_dir()):
        manifest_path = service_dir / "service.yaml"
        if not manifest_path.is_file():
            continue
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        selector = str(manifest.get("placement") or "")
        name = str(manifest.get("name") or service_dir.name)
        if selector:
            try:
                roles[name] = resolve_node_selector(config, selector)
            except ValueError:
                continue
    return roles


def _service_role(name: str, service: dict[str, Any], roles: dict[str, str]) -> str | None:
    if name in roles:
        return roles[name]
    matches = [candidate for candidate in roles if name.startswith(f"{candidate}-")]
    if matches:
        return roles[max(matches, key=len)]
    return None


def _compose_services(root: Path, cfg=None) -> dict[str, dict[str, Any]]:
    if cfg is not None:
        try:
            from toolkit.core.generate.compose_assemble import assemble_compose_text

            document = yaml.safe_load(assemble_compose_text(root, cfg, include_release=False)) or {}
        except (OSError, ValueError, yaml.YAMLError):
            return {}
    else:
        compose_path = root / "docker-compose.yml"
        if not compose_path.is_file():
            compose_path = root / "docker-compose.example.yml"
        if not compose_path.is_file():
            return {}
        try:
            document = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}
    services = document.get("services")
    if not isinstance(services, dict):
        return {}
    return {str(name): block for name, block in services.items() if isinstance(block, dict)}


def declared_container_names(root: Path) -> set[str]:
    services_dir = root / "toolkit" / "services"
    names = set(_manifest_nodes(services_dir))
    for service_name, service in _compose_services(root).items():
        if service_name.startswith("project-"):
            continue
        names.add(service_name)
        container_name = service.get("container_name")
        if isinstance(container_name, str) and container_name:
            names.add(container_name)
    return names


def declared_service_ports(root: Path, cfg=None) -> dict[str, list[tuple[str, int, str]]]:
    services_dir = root / "toolkit" / "services"
    if not services_dir.is_dir():
        return {}
    roles = _manifest_nodes(services_dir, cfg)
    by_role: dict[str, list[tuple[str, int, str]]] = {role: [] for role in set(roles.values())}
    compose_services = _compose_services(root, cfg)
    if compose_services:
        candidates = compose_services.items()
    else:
        fallback: dict[str, dict[str, Any]] = {}
        for name in roles:
            path = services_dir / name / "compose.yaml"
            if not path.is_file():
                continue
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            services = document.get("services") if isinstance(document, dict) else None
            if not isinstance(services, dict):
                continue
            for service_name, block in services.items():
                if isinstance(service_name, str) and isinstance(block, dict):
                    fallback[service_name] = block
        candidates = fallback.items()

    for service_name, service in candidates:
        if service_name.startswith("project-"):
            continue
        role = _service_role(service_name, service, roles)
        if role not in by_role:
            continue
        for port in compose_published_ports(service):
            if port.protocol not in {"tcp", "udp"}:
                continue
            if port.host_ip in {"127.0.0.1", "::1", "localhost"}:
                continue
            by_role[role].append((service_name, port.published, port.protocol))

    for role in by_role:
        by_role[role] = sorted(set(by_role[role]), key=lambda item: (item[1], item[2], item[0]))
    return by_role


def _service_catalog(root: Path) -> ServiceCatalog | None:
    from toolkit.core.manifest.catalog import load_service_catalog

    services_dir = root / "toolkit" / "services"
    if not services_dir.is_dir():
        services_dir = root
    if not any(services_dir.glob("*/service.yaml")):
        return None
    return load_service_catalog(services_dir)


def build_guest_firewall_policy(root: Path, raw: dict[str, Any]) -> list[FirewallRule]:
    """Compile platform and plugin contracts into least-privilege guest rules."""
    from toolkit.core.config.config import Config
    from toolkit.core.infra.edge_network import edge_network_values, prometheus_egress_network_values
    from toolkit.core.registry.mesh import mesh_lan_cidr

    cfg = Config.model_validate(raw)
    _, caddy_egress_ip = edge_network_values(cfg)
    _, prometheus_egress_ip = prometheus_egress_network_values(cfg)
    network = mesh_lan_cidr(cfg)
    mesh_cidr = cfg.network.mesh_ipv4_cidr
    node_ips = {node_id: machine.address for node_id, machine in cfg.machines.items() if machine.enabled}
    catalog = _service_catalog(root)

    def provider_nodes(capability: str, fallback_label: str) -> list[str]:
        if catalog is not None:
            from toolkit.core.manifest.placement import manifest_node
            from toolkit.core.manifest.routes import service_is_enabled

            manifest = catalog.provider(capability)
            if manifest is not None:
                if service_is_enabled(cfg, manifest, catalog):
                    return [manifest_node(cfg, manifest)]
        return [
            node_id for node_id, machine in cfg.machines.items() if machine.enabled and fallback_label in machine.labels
        ]

    ingress_nodes = provider_nodes("ingress", "ingress")
    mesh_router_nodes = provider_nodes("mesh-router", "control")
    rules: list[FirewallRule] = []

    def add(roles: list[str], source: str, port: int, proto: str, comment: str) -> None:
        if not roles:
            return
        rule: FirewallRule = {
            "roles": roles,
            "from_ip": source,
            "port": port,
            "proto": proto,
            "comment": comment,
        }
        if not any(
            existing["roles"] == roles
            and existing["from_ip"] == source
            and existing["port"] == port
            and existing["proto"] == proto
            for existing in rules
        ):
            rules.append(rule)

    all_roles = list(node_ips)
    gateway_roles: dict[str, list[str]] = {}
    for node_id in all_roles:
        gateway_roles.setdefault(cfg.machines[node_id].gateway, []).append(node_id)
    for gateway, roles in gateway_roles.items():
        add(roles, gateway, 22, "tcp", "SSH from Proxmox gateway")
    add(all_roles, mesh_cidr, 22, "tcp", "SSH from enrolled mesh")
    for router_node in mesh_router_nodes:
        router_ip = node_ips.get(router_node)
        if router_ip:
            add(
                [node_id for node_id in all_roles if node_id != router_node],
                router_ip,
                22,
                "tcp",
                "SSH through mesh subnet router",
            )
    web_sources = ("any",) if cfg.network.expose_via_internet else (network, mesh_cidr)
    for source in web_sources:
        add(ingress_nodes, source, 80, "tcp", "HTTP to Caddy")
        add(ingress_nodes, source, 443, "tcp", "HTTPS to Caddy")
    if catalog is not None:
        from toolkit.core.manifest.networking import compile_network_listeners
        from toolkit.core.manifest.placement import manifest_node, manifest_runtime_nodes
        from toolkit.core.manifest.routes import compile_routes, service_is_enabled

        enabled = tuple(manifest for manifest in catalog.manifests if service_is_enabled(cfg, manifest, catalog))
        enabled_names = {manifest.name for manifest in enabled}
        ingress_manifest = catalog.provider("ingress")

        # Caddy is the only HTTP client. Local traffic originates from its
        # isolated bridge address; cross-node traffic originates from the
        # ingress machine's private address.
        for route in compile_routes(cfg, catalog):
            if route.published_port is None or route.node not in node_ips:
                continue
            for ingress_node in ingress_nodes:
                source = caddy_egress_ip if ingress_node == route.node else node_ips[ingress_node]
                add(
                    [route.node],
                    source,
                    route.published_port,
                    "tcp",
                    f"{ingress_node} ingress to {route.service}",
                )

        # Typed service integrations and database bindings own cross-node
        # connectivity. Same-node consumers remain on the Compose network.
        for manifest in enabled:
            # A manifest can own a control-plane service and also declare
            # node-local runtimes (for example Alloy agents).  Integrations
            # originate from every concrete runtime placement, not only the
            # manifest's primary node.
            source_nodes = {manifest_node(cfg, manifest)}
            for runtime_service in manifest.runtimes:
                source_nodes.update(manifest_runtime_nodes(cfg, manifest, runtime_service))
            providers = {
                integration.service for integration in manifest.integrations if integration.service in enabled_names
            }
            providers.update(binding.provider for binding in manifest.databases if binding.provider in enabled_names)
            for provider_name in sorted(providers):
                provider = catalog.require(provider_name)
                target_node = manifest_node(cfg, provider)
                endpoint = provider.service_endpoint
                if endpoint is None or endpoint.published_port is None:
                    continue
                for source_node in sorted(source_nodes):
                    if source_node == target_node:
                        continue
                    add(
                        [target_node],
                        node_ips[source_node],
                        endpoint.published_port,
                        "tcp",
                        f"{source_node} services to integration {provider.name}",
                    )

        # The metrics provider is the sole client for every declared scrape port.
        prometheus = next((manifest for manifest in enabled if "metrics" in manifest.provides), None)
        if prometheus is not None:
            from toolkit.core.manifest.monitoring import compile_prometheus_targets

            prometheus_node = manifest_node(cfg, prometheus)
            for scrape_target in compile_prometheus_targets(cfg, catalog):
                if scrape_target.node is not None and scrape_target.host_port is not None:
                    source = (
                        prometheus_egress_ip if prometheus_node == scrape_target.node else node_ips[prometheus_node]
                    )
                    add(
                        [scrape_target.node],
                        source,
                        scrape_target.host_port,
                        "tcp",
                        f"prometheus scrape of {scrape_target.service}",
                    )

        # Non-HTTP host listeners are owned explicitly by their service plugin.
        for listener in compile_network_listeners(cfg, catalog):
            for target_node in listener.target_nodes:
                if (
                    ingress_manifest is not None
                    and listener.service in ingress_manifest.depends_on
                    and target_node in ingress_nodes
                ):
                    add(
                        [target_node],
                        caddy_egress_ip,
                        listener.port,
                        listener.protocol,
                        f"local ingress dependency {listener.service}",
                    )
                for source in listener.sources:
                    if target_node in node_ips and source != node_ips[target_node]:
                        add(
                            [target_node],
                            source,
                            listener.port,
                            listener.protocol,
                            f"{listener.service} {listener.listener_id}",
                        )

    from toolkit.core.projects.placement import project_node

    for project in cfg.projects.entries:
        target = project_node(cfg, project)
        for ingress_node in ingress_nodes:
            source = caddy_egress_ip if ingress_node == target else node_ips[ingress_node]
            add(
                [target],
                source,
                project.container_port,
                "tcp",
                f"{ingress_node} to project {project.subdomain}",
            )

        database_service = project.database_service
        if database_service and catalog is not None:
            from toolkit.core.manifest.placement import manifest_node

            try:
                provider = catalog.require(database_service)
            except KeyError:
                continue
            contract = provider.database_provider
            endpoint = provider.service_endpoint
            provider_node = manifest_node(cfg, provider)
            if (
                contract is not None
                and endpoint is not None
                and endpoint.published_port is not None
                and provider_node != target
            ):
                add(
                    [provider_node],
                    node_ips[target],
                    endpoint.published_port,
                    "tcp",
                    f"{target} projects to database {database_service}",
                )

    return rules
