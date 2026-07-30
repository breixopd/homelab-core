"""Lightweight mesh status for UI and CLI (no full hook verify)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


@dataclass(frozen=True)
class MeshStatusSnapshot:
    enabled: bool
    login_server: str
    lan_cidr: str
    subnet_router_ok: bool | None
    subnet_router_detail: str
    nodes_online: int | None
    nodes_total: int | None
    nodes_detail: str


def mesh_status_snapshot(cfg: Config, root: Path) -> MeshStatusSnapshot:
    """Headscale + subnet-router summary for operations UI."""
    from toolkit.core.registry.mesh import mesh_lan_cidr, mesh_login_server

    login = mesh_login_server(cfg)
    cidr = mesh_lan_cidr(cfg)
    if not cfg.category_enabled("security"):
        return MeshStatusSnapshot(
            enabled=False,
            login_server=login,
            lan_cidr=cidr,
            subnet_router_ok=None,
            subnet_router_detail="Headscale disabled",
            nodes_online=None,
            nodes_total=None,
            nodes_detail="not enabled",
        )

    from toolkit.core.manifest.placement import service_address
    from toolkit.services.headscale.plugin import check_nodes, check_subnet_router

    headscale_address = service_address(cfg, "headscale")
    router = check_subnet_router(cfg, headscale_address, root)
    nodes = check_nodes(cfg, headscale_address, root)
    online = nodes_total = None
    if nodes.passed and "/" in nodes.detail:
        try:
            online_s, total_s = nodes.detail.split("/", 1)
            online = int(online_s.split()[0])
            nodes_total = int(total_s.split()[0])
        except (ValueError, IndexError):
            pass
    return MeshStatusSnapshot(
        enabled=True,
        login_server=login,
        lan_cidr=cidr,
        subnet_router_ok=router.passed,
        subnet_router_detail=router.detail,
        nodes_online=online,
        nodes_total=nodes_total,
        nodes_detail=nodes.detail,
    )
