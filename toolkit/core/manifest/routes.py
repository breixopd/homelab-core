"""Compile service manifests into immutable, configuration-resolved routes."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from toolkit.core.config.config import Config
from toolkit.core.manifest.catalog import ServiceCatalog, load_service_catalog
from toolkit.core.manifest.schema import (
    ConfigPredicate,
    Exposure,
    ResponseHeader,
    RouteAuth,
    RouteMatch,
    ServiceManifest,
)


class RouteCompilationError(RuntimeError):
    pass


def route_fqdn(subdomain: str | None, domain: str) -> str:
    """Build a route FQDN, using an empty subdomain for the zone apex."""
    if subdomain is None:
        raise ValueError("subdomain must be resolved before building an FQDN")
    value = subdomain.strip()
    return domain if not value else f"{value}.{domain}"


@dataclass(frozen=True, slots=True)
class CompiledRoute:
    service: str
    category: str
    node: str
    label: str
    subdomain: str
    host: str
    upstream: str
    published_port: int | None
    compose_service: str
    exposure: Exposure
    auth: RouteAuth
    match: RouteMatch | None
    file_server_root: str
    response_body: str
    request_body_max_mb: int | None
    deny: tuple[RouteMatch, ...]
    response_headers: tuple[ResponseHeader, ...]


def _predicate_value(
    cfg: Config,
    predicate: ConfigPredicate,
    catalog: ServiceCatalog,
) -> bool | str | int | float | None:
    if predicate.setting is not None:
        from toolkit.core.manifest.settings import service_setting_value

        service, key = predicate.setting.split(".", 1)
        try:
            manifest = catalog.require(service)
        except KeyError as exc:
            raise RouteCompilationError(f"predicate references unknown service {service!r}") from exc
        return service_setting_value(cfg, manifest, key)

    if predicate.path is None:
        raise RouteCompilationError("predicate must declare exactly one of setting or path")
    value: object = cfg
    for part in predicate.path.split("."):
        if not isinstance(value, BaseModel) or part not in type(value).model_fields:
            raise RouteCompilationError(f"predicate {predicate.path!r} may traverse Pydantic fields only")
        value = getattr(value, part)
    if value is not None and not isinstance(value, bool | str | int | float):
        raise RouteCompilationError(f"predicate {predicate.path!r} did not resolve to a scalar")
    return value


def predicate_matches(cfg: Config, predicate: ConfigPredicate, catalog: ServiceCatalog) -> bool:
    """Evaluate one declarative predicate against typed desired state."""
    value = _predicate_value(cfg, predicate, catalog)
    if "equals" in predicate.model_fields_set:
        return value == predicate.equals
    return value in predicate.one_of


def service_is_enabled(
    cfg: Config,
    manifest: ServiceManifest,
    catalog: ServiceCatalog | None = None,
) -> bool:
    """Resolve category and service predicates without executable configuration."""
    if not cfg.category_enabled(manifest.category):
        return False
    enabled_setting = next(
        (setting for setting in manifest.management.settings if setting.key == "enabled"),
        None,
    )
    if enabled_setting is not None:
        from toolkit.core.manifest.settings import service_setting_value

        if enabled_setting.type != "boolean":
            raise RouteCompilationError("the reserved enabled service setting must be boolean")
        if not service_setting_value(cfg, manifest, "enabled"):
            return False
    selected = catalog or load_service_catalog()
    return all(predicate_matches(cfg, predicate, selected) for predicate in manifest.enabled_when)


def compile_routes(cfg: Config, catalog: ServiceCatalog | None = None) -> tuple[CompiledRoute, ...]:
    from toolkit.core.manifest.placement import manifest_node

    selected = catalog or load_service_catalog()
    compiled: list[CompiledRoute] = []
    for manifest in selected.manifests:
        if not service_is_enabled(cfg, manifest, selected):
            continue
        for route in manifest.routes:
            matching_variants = [
                variant for variant in route.variants if predicate_matches(cfg, variant.when, selected)
            ]
            if len(matching_variants) > 1:
                raise RouteCompilationError(f"route for {manifest.name!r} matched multiple variants")
            variant = matching_variants[0] if matching_variants else None
            upstream = variant.upstream if variant is not None else route.upstream
            if not route.file_server_root and not route.response_body and not upstream:
                raise RouteCompilationError(f"route for {manifest.name!r} has no active upstream")
            compose_service = (
                (variant.compose_service if variant is not None else "") or route.compose_service or manifest.name
            )
            subdomain = manifest.name if route.subdomain is None else route.subdomain
            host = cfg.domain if not subdomain else f"{subdomain}.{cfg.domain}"
            exposure: Exposure = route.exposure
            if exposure == "public" and not cfg.network.expose_via_internet:
                exposure = "private"
            compiled.append(
                CompiledRoute(
                    service=manifest.name,
                    category=manifest.category,
                    node=manifest_node(cfg, manifest),
                    label=manifest.label,
                    subdomain=subdomain,
                    host=host,
                    upstream=upstream,
                    published_port=route.published_port,
                    compose_service=compose_service,
                    exposure=exposure,
                    auth=route.auth,
                    match=route.match,
                    file_server_root=route.file_server_root,
                    response_body=route.response_body.replace("{domain}", cfg.domain),
                    request_body_max_mb=route.request_body_max_mb,
                    deny=route.deny,
                    response_headers=route.response_headers,
                )
            )
    for project in cfg.projects.entries:
        from toolkit.core.projects.placement import project_node

        project_exposure: Exposure = project.exposure
        if project_exposure == "public" and not cfg.network.expose_via_internet:
            project_exposure = "private"
        compiled.append(
            CompiledRoute(
                service=f"project-{project.subdomain}",
                category="projects",
                node=project_node(cfg, project),
                label=project.name or project.subdomain,
                subdomain=project.subdomain,
                host=f"{project.subdomain}.{cfg.domain}",
                upstream=project.upstream,
                published_port=project.container_port,
                compose_service=f"project-{project.subdomain}",
                exposure=project_exposure,
                auth=RouteAuth(mode=project.auth_mode),
                match=None,
                file_server_root="",
                response_body="",
                request_body_max_mb=None,
                deny=(),
                response_headers=(),
            )
        )
    return tuple(compiled)


def public_routes(cfg: Config, catalog: ServiceCatalog | None = None) -> tuple[CompiledRoute, ...]:
    return tuple(route for route in compile_routes(cfg, catalog) if route.exposure == "public")


def private_routes(cfg: Config, catalog: ServiceCatalog | None = None) -> tuple[CompiledRoute, ...]:
    return tuple(route for route in compile_routes(cfg, catalog) if route.exposure == "private")


def managed_route_hosts(cfg: Config, catalog: ServiceCatalog | None = None) -> frozenset[str]:
    """Return every service/project hostname owned by route reconciliation.

    Disabled and public services remain in this ownership set so a previous
    private rewrite can be removed without pruning unrelated fleet or mesh DNS.
    """
    selected = catalog or load_service_catalog()
    hosts = {
        route_fqdn(manifest.name if route.subdomain is None else route.subdomain, cfg.domain)
        for manifest in selected.manifests
        for route in manifest.routes
    }
    hosts.update(f"{project.subdomain}.{cfg.domain}" for project in cfg.projects.entries)
    return frozenset(hosts)


def route_scope(route: CompiledRoute) -> str:
    """Render a compact, truthful path scope for control surfaces."""
    if route.match is not None:
        suffix = "*" if route.match.kind == "prefix" else ""
        examples = [f"{path[:120]}{'...' if len(path) > 120 else ''}{suffix}" for path in route.match.paths[:3]]
        if len(route.match.paths) > len(examples):
            examples.append(f"+{len(route.match.paths) - len(examples)} more")
        scope = f"{route.match.kind}: {', '.join(examples)}"
        return f"{scope[:497]}..." if len(scope) > 500 else scope
    if route.auth.mode == "split":
        return f"default; {len(route.auth.passthrough_paths)} exact native paths"
    return "default"
