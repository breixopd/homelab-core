"""Deterministic Docker edge network selection without LAN overlap."""

from __future__ import annotations

import ipaddress

from toolkit.core.config.config import Config
from toolkit.core.registry.mesh import mesh_lan_cidr


def edge_network_values(config: Config) -> tuple[str, str]:
    private = ipaddress.ip_network(mesh_lan_cidr(config), strict=False)
    container_pool = ipaddress.ip_network(config.network.container_ipv4_cidr)
    candidates = (
        ipaddress.ip_network("172.31.250.0/24"),
        ipaddress.ip_network("172.30.250.0/24"),
        ipaddress.ip_network("10.255.250.0/24"),
    )
    subnet = next(
        candidate
        for candidate in candidates
        if not candidate.overlaps(private) and not candidate.overlaps(container_pool)
    )
    return str(subnet), str(subnet.network_address + 2)


def prometheus_egress_network_values(config: Config) -> tuple[str, str]:
    """Return a dedicated collector subnet that cannot reach Caddy's bridge."""
    private = ipaddress.ip_network(mesh_lan_cidr(config), strict=False)
    container_pool = ipaddress.ip_network(config.network.container_ipv4_cidr)
    edge_subnet = ipaddress.ip_network(edge_network_values(config)[0])
    candidates = (
        ipaddress.ip_network("172.31.249.0/24"),
        ipaddress.ip_network("172.30.249.0/24"),
        ipaddress.ip_network("10.255.249.0/24"),
    )
    subnet = next(
        candidate
        for candidate in candidates
        if not candidate.overlaps(private)
        and not candidate.overlaps(container_pool)
        and not candidate.overlaps(edge_subnet)
    )
    return str(subnet), str(subnet.network_address + 2)
