"""Caddy reverse-proxy helpers — cfg-aware."""

from __future__ import annotations

from typing import TYPE_CHECKING

from toolkit.services.sdk.authelia import authelia_forward_auth_block

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

__all__ = [
    "caddy_forward_auth_block",
    "caddy_cross_vm_upstream",
    "caddy_reload_cmd",
]


def caddy_forward_auth_block(cfg: Config) -> list[str]:
    """Caddy ``forward_auth`` block lines for Authelia."""
    return authelia_forward_auth_block(cfg)


def caddy_cross_vm_upstream(cfg: Config, vm: str, port: str, *, published_port: int | None = None) -> str:
    """Rewrite a Docker upstream to ``<vm_ip>:<published_port>`` for cross-VM Caddy."""
    published = str(published_port) if published_port is not None else port
    return f"{cfg.node_ip(vm)}{':' + published if published else ''}"


def caddy_reload_cmd() -> list[str]:
    """Docker exec argv to reload Caddy in-place."""
    return ["docker", "exec", "caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile"]
