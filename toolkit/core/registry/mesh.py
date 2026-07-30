"""Headscale mesh helpers — ACL tags, preauth keys, personal vs fleet enrollment."""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def mesh_lan_cidr(cfg: Config) -> str:
    """Private network advertised by the manifest-selected mesh router."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_node

    router = load_service_catalog().require_provider("mesh-router")
    machine = cfg.machines[manifest_node(cfg, router)]
    return str(ipaddress.ip_network(f"{machine.address}/{machine.cidr}", strict=False))


def mesh_router_tag(cfg: Config) -> str:
    return (getattr(cfg.fleet, "mesh_router_tag", None) or "tag:homelab-router").strip()


def headscale_acl_tags(cfg: Config) -> list[str]:
    """All Headscale ACL tag-owner entries declared by desired state."""
    tags = set(cfg.fleet.headscale_tags or ["tag:fleet-external"])
    if getattr(cfg.fleet, "mesh_subnet_router", True):
        tags.add(mesh_router_tag(cfg))
    for host in cfg.external_hosts:
        if host.kind == "fleet":
            tags.update(host.headscale_tags)
    return sorted(tags)


def headscale_tag_owner(cfg: Config) -> str:
    """Principal that owns fleet tags in ACL policy (Headscale user@ form)."""
    owner = (getattr(cfg.fleet, "headscale_tag_owner", None) or "").strip()
    if owner:
        return owner if "@" in owner else f"{owner}@"
    # Preauth keys are created for the bootstrap Headscale user (homelab@).
    return "homelab@"


def preauth_key_tags_match(key_row: dict, requested: list[str] | None) -> bool:
    """True when a listed preauth key carries exactly the requested tag set."""
    req = sorted({t.strip() for t in (requested or []) if t.strip()})
    key_tags = key_row.get("acl_tags") or key_row.get("aclTags") or key_row.get("tags") or []
    if not isinstance(key_tags, list):
        key_tags = [key_tags]
    got = sorted({str(t).strip() for t in key_tags if str(t).strip()})
    return got == req


def mesh_login_server(cfg: Config) -> str:
    """Public Headscale URL for personal devices and fleet nodes."""
    return f"https://vpn.{cfg.domain}"


def mesh_infra_login_server(cfg: Config) -> str:
    """Private-DNS Headscale URL used by the infra subnet router."""
    return mesh_login_server(cfg)


def resolve_headscale_login_server(cfg: Config, *, on_infra_host: bool = False) -> str:
    if on_infra_host and cfg.is_multi_node:
        return mesh_infra_login_server(cfg)
    return mesh_login_server(cfg)


def personal_mesh_up_args(cfg: Config, *, hostname: str = "homelab-controller") -> list[str]:
    """Tailscale args for OIDC personal enrollment (shows your Authelia user, not tagged-devices)."""
    login = mesh_login_server(cfg)
    return [
        "tailscale",
        "up",
        f"--login-server={login}",
        f"--hostname={hostname}",
        "--accept-routes",
        "--reset",
    ]
