from __future__ import annotations

import pytest
from toolkit.core.config.config import Config, ProjectEntry, ProjectsConfig
from toolkit.core.manifest.catalog import ServiceCatalog
from toolkit.core.manifest.routes import RouteCompilationError, compile_routes, route_scope
from toolkit.core.manifest.schema import ServiceManifest


def _service(
    name: str,
    *,
    exposure: str = "public",
    enabled_when: list[dict] | None = None,
    variants: list[dict] | None = None,
    management: dict | None = None,
    routes: bool = True,
) -> ServiceManifest:
    return ServiceManifest.model_validate(
        {
            "name": name,
            "label": name.title(),
            "description": f"{name} service",
            "icon": "box",
            "category": "media",
            "placement": "media",
            "priority": 20,
            "enabled_when": enabled_when or [],
            "management": management or {},
            "routes": [
                {
                    "upstream": f"{name}:8080",
                    "exposure": exposure,
                    "auth": {"mode": "native"},
                    "variants": variants or [],
                }
            ]
            if routes
            else [],
        }
    )


def _media_library() -> ServiceManifest:
    return _service(
        "media-library",
        routes=False,
        management={
            "settings": [
                {
                    "key": "server",
                    "label": "Media server",
                    "type": "select",
                    "default": "jellyfin",
                    "choices": ["jellyfin", "plex", "both"],
                }
            ]
        },
    )


def test_compile_routes_resolves_hosts_defaults_and_internet_policy() -> None:
    service = _service("example")
    catalog = ServiceCatalog((service,))

    route = compile_routes(Config(domain="home.example"), catalog)[0]
    private_route = compile_routes(
        Config(domain="home.example", network={"expose_via_internet": False}),
        catalog,
    )[0]

    assert route.host == "example.home.example"
    assert route.subdomain == "example"
    assert route.compose_service == "example"
    assert route.exposure == "public"
    assert private_route.exposure == "private"


def test_compile_routes_omits_services_with_false_predicates() -> None:
    jellyfin = _service(
        "jellyfin",
        enabled_when=[{"setting": "media-library.server", "one_of": ["jellyfin", "both"]}],
    )
    catalog = ServiceCatalog((_media_library(), jellyfin))

    plex_config = Config(domain="home.example", service_settings={"media-library": {"server": "plex"}})
    both_config = Config(domain="home.example", service_settings={"media-library": {"server": "both"}})

    assert compile_routes(plex_config, catalog) == ()
    assert compile_routes(both_config, catalog)[0].service == "jellyfin"


def test_compile_routes_supports_finite_float_config_predicates() -> None:
    service = _service(
        "example",
        enabled_when=[{"path": "storage.zfs_overhead_pct", "equals": 2.5}],
    )
    catalog = ServiceCatalog((service,))

    assert compile_routes(Config(storage={"zfs_overhead_pct": 2.0}), catalog) == ()
    assert compile_routes(Config(storage={"zfs_overhead_pct": 2.5}), catalog)[0].service == "example"


def test_compile_routes_selects_one_typed_variant() -> None:
    qbittorrent = _service(
        "qbittorrent",
        variants=[
            {
                "when": {"setting": "gluetun.enabled", "equals": True},
                "upstream": "gluetun:8080",
                "compose_service": "qbittorrent",
            }
        ],
    )
    gluetun = _service(
        "gluetun",
        routes=False,
        management={"settings": [{"key": "enabled", "label": "Enabled", "type": "boolean", "default": True}]},
    )
    catalog = ServiceCatalog((gluetun, qbittorrent))

    direct = compile_routes(Config(domain="home.example", service_settings={"gluetun": {"enabled": False}}), catalog)[0]
    vpn = compile_routes(Config(domain="home.example"), catalog)[0]

    assert direct.upstream == "qbittorrent:8080"
    assert vpn.upstream == "gluetun:8080"
    assert vpn.compose_service == "qbittorrent"


def test_compile_routes_rejects_ambiguous_variants() -> None:
    media_library = _media_library()
    service = _service(
        "example",
        variants=[
            {"when": {"setting": "media-library.server", "equals": "jellyfin"}, "upstream": "one:8080"},
            {"when": {"setting": "media-library.server", "one_of": ["jellyfin"]}, "upstream": "two:8080"},
        ],
    )

    with pytest.raises(RouteCompilationError, match="multiple variants"):
        compile_routes(Config(domain="home.example"), ServiceCatalog((media_library, service)))


def test_predicate_resolution_cannot_traverse_non_models() -> None:
    service = _service("example", enabled_when=[{"path": "network.model_dump", "equals": True}])

    with pytest.raises(RouteCompilationError, match="Pydantic fields"):
        compile_routes(Config(domain="home.example"), ServiceCatalog((service,)))


def test_compile_routes_includes_typed_dynamic_projects() -> None:
    cfg = Config(
        domain="home.example",
        projects=ProjectsConfig(
            entries=[
                ProjectEntry(
                    name="Demo",
                    subdomain="demo",
                    auth_mode="forward_auth",
                    exposure="private",
                    placement="apps",
                    docker_image="docker.io/example/demo:1@sha256:" + "a" * 64,
                    container_port=8080,
                )
            ]
        ),
    )

    route = next(
        route
        for route in compile_routes(cfg, ServiceCatalog((_service("example"),)))
        if route.host == "demo.home.example"
    )

    assert route.service == "project-demo"
    assert route.auth.mode == "forward_auth"
    assert route.exposure == "private"
    assert route.node == "apps"


def test_route_scope_is_bounded_for_large_path_policies() -> None:
    raw = _service("example").model_dump(mode="python")
    raw["routes"] = [
        *raw["routes"],
        {
            "upstream": "example:8080",
            "exposure": "public",
            "auth": {"mode": "native"},
            "match": {
                "kind": "exact",
                "paths": [f"/{letter}{'x' * 300}" for letter in "abcd"],
            },
        },
    ]
    catalog = ServiceCatalog((ServiceManifest.model_validate(raw),))
    matched = next(route for route in compile_routes(Config(domain="home.example"), catalog) if route.match)

    scope = route_scope(matched)

    assert len(scope) <= 500
    assert "+1 more" in scope
