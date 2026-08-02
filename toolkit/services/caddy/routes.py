"""Compile canonical routes into a host-oriented Caddy render model."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from toolkit.core.config.config import Config
from toolkit.core.manifest.routes import CompiledRoute
from toolkit.core.manifest.schema import AuthMode, Exposure, ResponseHeader
from toolkit.core.registry.mesh import mesh_lan_cidr
from toolkit.services.sdk import caddy_cross_vm_upstream

IDENTITY_REQUEST_HEADERS = (
    "Remote-User",
    "Remote-Groups",
    "Remote-Name",
    "Remote-Email",
    "X-Authenticated-User",
    "X-Auth-Request-User",
    "X-Auth-Request-Groups",
    "X-Auth-Request-Email",
    "X-Forwarded-User",
    "X-Forwarded-Groups",
    "X-Forwarded-Email",
)

HandlerKind = Literal["exact", "prefix", "default"]


class CaddyCompilationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CaddyHandler:
    kind: HandlerKind
    paths: tuple[str, ...]
    auth_mode: AuthMode | None
    upstream: str
    file_server_root: str
    response_body: str
    respond_status: int | None = None


@dataclass(frozen=True, slots=True)
class CaddyRoute:
    host: str
    exposure: Exposure
    allowed_remote_cidrs: tuple[str, ...]
    reject_untrusted_status: int | None
    identity_request_headers: tuple[str, ...]
    request_body_max_mb: int | None
    response_headers: tuple[ResponseHeader, ...]
    handlers: tuple[CaddyHandler, ...]


def _target(cfg: Config, route: CompiledRoute) -> str:
    if route.file_server_root or route.response_body:
        return ""
    _service, separator, port = route.upstream.partition(":")
    if not separator or not port:
        raise CaddyCompilationError(f"route {route.service!r} has an invalid upstream")
    if route.published_port is None:
        raise CaddyCompilationError(f"route {route.service!r} must declare a host-published port")
    return caddy_cross_vm_upstream(cfg, route.node, port, published_port=route.published_port)


def _proxy_handler(cfg: Config, route: CompiledRoute, kind: HandlerKind, paths: tuple[str, ...]) -> CaddyHandler:
    return CaddyHandler(
        kind=kind,
        paths=paths,
        auth_mode=route.auth.mode,
        upstream=_target(cfg, route),
        file_server_root=route.file_server_root,
        response_body=route.response_body,
    )


def _handler_sort_key(handler: CaddyHandler) -> tuple[int, int, str]:
    if handler.respond_status is not None:
        rank = 0 if handler.kind == "exact" else 1
    elif handler.kind == "exact":
        rank = 2
    elif handler.kind == "prefix":
        rank = 3
    else:
        rank = 4
    longest = max((len(path) for path in handler.paths), default=0)
    return (rank, -longest, handler.paths[0] if handler.paths else "")


def _compile_site(cfg: Config, host: str, routes: list[CompiledRoute]) -> CaddyRoute:
    exposures = {route.exposure for route in routes}
    if len(exposures) != 1:
        raise CaddyCompilationError(f"host {host!r} has inconsistent exposure policies")

    defaults = [route for route in routes if route.match is None]
    if len(defaults) != 1:
        raise CaddyCompilationError(f"host {host!r} requires exactly one default route")
    default = defaults[0]

    for route in routes:
        if route.match is not None and (route.request_body_max_mb is not None or route.response_headers or route.deny):
            raise CaddyCompilationError(f"host {host!r} has site policy on a path-scoped route")

    handlers: list[CaddyHandler] = []
    for denied in default.deny:
        handlers.append(
            CaddyHandler(
                kind=denied.kind,
                paths=denied.paths,
                auth_mode=None,
                upstream="",
                file_server_root="",
                response_body="",
                respond_status=403,
            )
        )

    for route in routes:
        if route.match is not None:
            handlers.append(_proxy_handler(cfg, route, route.match.kind, route.match.paths))

    if default.auth.mode == "split":
        handlers.append(
            CaddyHandler(
                kind="exact",
                paths=default.auth.passthrough_paths,
                auth_mode="native",
                upstream=_target(cfg, default),
                file_server_root=default.file_server_root,
                response_body=default.response_body,
            )
        )
        default_handler = _proxy_handler(cfg, default, "default", ())
        default_handler = CaddyHandler(
            kind=default_handler.kind,
            paths=default_handler.paths,
            auth_mode="forward_auth",
            upstream=default_handler.upstream,
            file_server_root=default_handler.file_server_root,
            response_body=default_handler.response_body,
        )
    else:
        default_handler = _proxy_handler(cfg, default, "default", ())
    handlers.append(default_handler)
    handlers.sort(key=_handler_sort_key)

    exposure = exposures.pop()
    allowed = (mesh_lan_cidr(cfg), cfg.network.mesh_ipv4_cidr) if exposure == "private" else ()
    return CaddyRoute(
        host=host,
        exposure=exposure,
        allowed_remote_cidrs=allowed,
        reject_untrusted_status=404 if allowed else None,
        identity_request_headers=IDENTITY_REQUEST_HEADERS,
        request_body_max_mb=default.request_body_max_mb,
        response_headers=default.response_headers,
        handlers=tuple(handlers),
    )


def compile_caddy_routes(cfg: Config, routes: tuple[CompiledRoute, ...]) -> tuple[CaddyRoute, ...]:
    """Group immutable routes by host and derive deterministic Caddy policies."""
    by_host: dict[str, list[CompiledRoute]] = defaultdict(list)
    for route in routes:
        by_host[route.host].append(route)
    return tuple(_compile_site(cfg, host, by_host[host]) for host in sorted(by_host))
