"""Resolve runtime placement from strict service and machine manifests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.catalog import ServiceCatalog
    from toolkit.core.manifest.schema import ServiceManifest


def service_endpoint_port(
    service: str,
    *,
    published: bool = False,
    catalog: ServiceCatalog | None = None,
) -> int:
    """Return a manifest-owned service endpoint port.

    ``published`` selects the host-facing port used across machine boundaries;
    the container port is used for Compose-local traffic. Keeping this lookup
    in the manifest layer prevents plugins and framework helpers from copying
    service-specific port literals.
    """
    from toolkit.core.manifest.catalog import load_service_catalog

    selected = catalog or load_service_catalog()
    manifest = selected.require(service)
    endpoint = manifest.service_endpoint
    if endpoint is None:
        raise ValueError(f"service {service!r} does not declare a service endpoint")
    if published:
        if endpoint.published_port is None:
            raise ValueError(f"service {service!r} does not declare a published endpoint port")
        return endpoint.published_port
    return endpoint.container_port


def service_route_port(
    service: str,
    *,
    subdomain: str | None = None,
    published: bool = False,
    catalog: ServiceCatalog | None = None,
) -> int:
    """Return the static upstream or published port for one manifest route."""
    from toolkit.core.manifest.catalog import load_service_catalog

    selected = catalog or load_service_catalog()
    manifest = selected.require(service)
    routes = [route for route in manifest.routes if subdomain is None or route.subdomain == subdomain]
    if len(routes) != 1:
        qualifier = f" for subdomain {subdomain!r}" if subdomain is not None else ""
        raise ValueError(f"service {service!r} must declare exactly one static route{qualifier}")
    route = routes[0]
    if published:
        if route.published_port is None:
            raise ValueError(f"service {service!r} route does not declare a published port")
        return route.published_port
    if not route.upstream or ":" not in route.upstream:
        raise ValueError(f"service {service!r} route does not declare a static upstream port")
    return int(route.upstream.rsplit(":", 1)[1])


def resolve_node_selector(config: Config, selector: str, *, enabled_only: bool = True) -> str:
    """Resolve an exact machine ID or a unique machine capability label."""
    exact = config.machines.get(selector)
    if exact is not None and (exact.enabled or not enabled_only):
        return selector
    matches = sorted(
        machine_id
        for machine_id, machine in config.machines.items()
        if (machine.enabled or not enabled_only) and selector in machine.labels
    )
    if not matches:
        raise ValueError(f"placement {selector!r} matches no enabled machine")
    if len(matches) > 1:
        raise ValueError(f"placement {selector!r} matches multiple enabled machines: {', '.join(matches)}")
    return matches[0]


def manifest_node(config: Config, manifest: ServiceManifest) -> str:
    """Resolve a service manifest's primary placement to a concrete node ID."""
    if not config.is_multi_node:
        return config.control_node
    return resolve_node_selector(config, manifest.placement)


def manifest_runtime_nodes(
    config: Config,
    manifest: ServiceManifest,
    runtime_service: str,
    *,
    primary_node: str | None = None,
) -> tuple[str, ...]:
    """Resolve a Compose service's declared runtime placements."""
    runtime = manifest.runtimes.get(runtime_service)
    if runtime is None or not runtime.placements:
        return (primary_node or manifest_node(config, manifest),)
    selectors = runtime.placements
    primary = primary_node or manifest_node(config, manifest)
    resolved: list[str] = []
    for selector in selectors:
        if selector == "@primary":
            resolved.append(primary)
            continue
        if selector == "@all":
            resolved.extend(config.enabled_nodes)
            continue
        if selector == "@non-primary":
            resolved.extend(node for node in config.enabled_nodes if node != primary)
            continue
        exact = config.machines.get(selector)
        if exact is not None:
            if exact.enabled:
                resolved.append(selector)
            continue
        configured_matches = [
            machine_id for machine_id, machine in config.machines.items() if selector in machine.labels
        ]
        if not configured_matches:
            raise ValueError(f"runtime placement {selector!r} matches no configured machine")
        enabled_matches = set(config.enabled_nodes) & set(configured_matches)
        resolved.extend(node for node in config.enabled_nodes if node in enabled_matches)
    return tuple(dict.fromkeys(resolved))


def runtime_profiles_for_node(config: Config, catalog: ServiceCatalog, node: str) -> tuple[str, ...]:
    """Compile explicitly activated Compose profiles for placed service runtimes."""
    from toolkit.core.manifest.routes import service_is_enabled

    profiles = {
        runtime.compose_profile
        for manifest in catalog.manifests
        if service_is_enabled(config, manifest)
        for runtime_service, runtime in manifest.runtimes.items()
        if runtime.compose_profile and node in manifest_runtime_nodes(config, manifest, runtime_service)
    }
    return tuple(sorted(profiles))


def manifest_storage_nodes(config: Config, manifest: ServiceManifest, runtime_service: str) -> tuple[str, ...]:
    """Resolve the concrete node set that owns one storage asset."""
    if runtime_service:
        return manifest_runtime_nodes(config, manifest, runtime_service)
    return (manifest_node(config, manifest),)


def service_node_map(config: Config, catalog: ServiceCatalog) -> dict[str, str]:
    """Resolve every manifest owner once for generation and inventory compilation."""
    from toolkit.core.manifest.routes import service_is_enabled

    resolved: dict[str, str] = {}
    for manifest in catalog.manifests:
        if service_is_enabled(config, manifest):
            resolved[manifest.name] = manifest_node(config, manifest)
            continue
        try:
            resolved[manifest.name] = resolve_node_selector(
                config,
                manifest.placement,
                enabled_only=False,
            )
        except ValueError:
            resolved[manifest.name] = config.control_node
    return resolved


def category_node(config: Config, placement: str) -> str:
    """Resolve a category's default placement selector."""
    if not config.is_multi_node:
        return config.control_node
    return resolve_node_selector(config, placement)


def service_node(config: Config, service: str) -> str:
    """Return the enabled machine that owns a service at runtime."""
    if not config.is_multi_node:
        return config.control_node

    from toolkit.core.manifest.catalog import load_service_catalog

    manifest = load_service_catalog().require(service)
    try:
        return manifest_node(config, manifest)
    except ValueError as exc:
        raise ValueError(f"service {service!r} has invalid placement: {exc}") from exc


def service_address(config: Config, service: str) -> str:
    """Return the configured address of the machine hosting a service."""
    return config.node_ip(service_node(config, service))


def service_is_local(config: Config, current_node: str, service: str) -> bool:
    """Return whether a service is co-located with the current runtime node."""
    return not config.is_multi_node or current_node == service_node(config, service)
