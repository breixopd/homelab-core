from __future__ import annotations

from dataclasses import replace

import pytest
from tests.helpers.machines import renamed_default_machines
from toolkit.core.config.config import Config
from toolkit.core.manifest.routes import CompiledRoute
from toolkit.core.manifest.schema import ResponseHeader, RouteAuth, RouteMatch
from toolkit.services.caddy.routes import (
    IDENTITY_REQUEST_HEADERS,
    CaddyCompilationError,
    compile_caddy_routes,
)


def _route(
    *,
    service: str = "example",
    host: str = "example.home.test",
    node: str = "infra",
    upstream: str = "example:8080",
    published_port: int | None = 8080,
    exposure: str = "public",
    auth: str = "forward_auth",
    match: RouteMatch | None = None,
    passthrough_paths: tuple[str, ...] = (),
    request_body_max_mb: int | None = None,
    deny: tuple[RouteMatch, ...] = (),
    response_headers: tuple[ResponseHeader, ...] = (),
) -> CompiledRoute:
    return CompiledRoute(
        service=service,
        category="management",
        node=node,
        label=service.title(),
        subdomain=host.split(".", 1)[0],
        host=host,
        upstream=upstream,
        published_port=published_port,
        compose_service=service,
        exposure=exposure,  # type: ignore[arg-type]
        auth=RouteAuth(mode=auth, passthrough_paths=passthrough_paths),  # type: ignore[arg-type]
        match=match,
        file_server_root="",
        request_body_max_mb=request_body_max_mb,
        deny=deny,
        response_headers=response_headers,
    )


def test_compile_caddy_groups_secondary_routes_before_authenticated_default() -> None:
    native = _route(
        service="gitea",
        host="git.home.test",
        upstream="gitea:3000",
        auth="native",
        match=RouteMatch(kind="prefix", paths=("/v2/",)),
    )
    default = replace(native, auth=RouteAuth(mode="forward_auth"), match=None)

    site = compile_caddy_routes(Config(domain="home.test"), (native, default))[0]

    assert site.host == "git.home.test"
    assert [handler.kind for handler in site.handlers] == ["prefix", "default"]
    assert site.handlers[0].paths == ("/v2/",)
    assert site.handlers[0].auth_mode == "native"
    assert site.handlers[1].auth_mode == "forward_auth"
    assert site.identity_request_headers == IDENTITY_REQUEST_HEADERS


def test_compile_caddy_expands_split_auth_as_exact_native_then_forward_auth() -> None:
    route = _route(
        service="fmd-server",
        host="fmd.home.test",
        upstream="fmd-server:8080",
        auth="split",
        passthrough_paths=("/version", "/api/v1/location"),
        request_body_max_mb=15,
    )

    site = compile_caddy_routes(Config(domain="home.test"), (route,))[0]

    assert [(handler.kind, handler.auth_mode) for handler in site.handlers] == [
        ("exact", "native"),
        ("default", "forward_auth"),
    ]
    assert site.handlers[0].paths == ("/version", "/api/v1/location")
    assert "/api/v1/unknown" not in site.handlers[0].paths
    assert site.request_body_max_mb == 15


def test_compile_caddy_private_sites_restrict_the_immediate_peer() -> None:
    site = compile_caddy_routes(
        Config(domain="home.test"),
        (_route(exposure="private"),),
    )[0]

    assert site.allowed_remote_cidrs == ("10.10.10.0/24", "100.64.0.0/10")
    assert site.reject_untrusted_status == 404


def test_compile_caddy_rewrites_cross_vm_upstreams() -> None:
    site = compile_caddy_routes(
        Config(domain="home.test"),
        (_route(service="vaultwarden", node="apps", upstream="vaultwarden:80", published_port=8082),),
    )[0]

    assert site.handlers[0].upstream == "10.10.10.12:8082"


def test_compile_caddy_rewrites_fmd_to_its_private_apps_port() -> None:
    site = compile_caddy_routes(
        Config(domain="home.test"),
        (_route(service="fmd-server", node="apps", upstream="fmd-server:8080", published_port=8084),),
    )[0]

    assert site.handlers[0].upstream == "10.10.10.12:8084"


def test_compile_caddy_rejects_route_without_host_publication() -> None:
    with pytest.raises(CaddyCompilationError, match="host-published port"):
        compile_caddy_routes(
            Config(domain="home.test"),
            (_route(published_port=None),),
        )


def test_manifest_published_port_survives_machine_rename() -> None:
    from toolkit.core.manifest.routes import compile_routes

    cfg = Config(domain="home.test", machines=renamed_default_machines())
    route = next(route for route in compile_routes(cfg) if route.service == "fmd-server")

    site = compile_caddy_routes(cfg, (route,))[0]

    assert route.node == "data"
    assert route.published_port == 8084
    assert site.handlers[0].upstream == "10.10.10.12:8084"


def test_compile_caddy_orders_deny_policies_before_route_handlers() -> None:
    route = _route(
        deny=(
            RouteMatch(kind="prefix", paths=("/admin/",)),
            RouteMatch(kind="exact", paths=("/admin",)),
        ),
        response_headers=(ResponseHeader(name="Content-Security-Policy", value="default-src 'self'"),),
    )

    site = compile_caddy_routes(Config(domain="home.test"), (route,))[0]

    assert [(handler.kind, handler.respond_status) for handler in site.handlers] == [
        ("exact", 403),
        ("prefix", 403),
        ("default", None),
    ]
    assert site.response_headers[0].name == "Content-Security-Policy"


def test_compile_caddy_rejects_host_policy_drift() -> None:
    default = _route()
    secondary = replace(
        default,
        exposure="private",
        auth=RouteAuth(mode="native"),
        match=RouteMatch(kind="exact", paths=("/api",)),
    )

    with pytest.raises(CaddyCompilationError, match="exposure"):
        compile_caddy_routes(Config(domain="home.test"), (secondary, default))
