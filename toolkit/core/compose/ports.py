"""Normalize published Compose ports for validation and policy compilation."""

from __future__ import annotations

from typing import Any, NamedTuple


class PublishedPort(NamedTuple):
    host_ip: str
    published: int
    target: int
    protocol: str


def compose_published_ports(service: dict[str, Any]) -> list[PublishedPort]:
    parsed: list[PublishedPort] = []
    declarations = service.get("ports")
    if not isinstance(declarations, list):
        return parsed
    for declaration in declarations:
        host_ip = ""
        protocol = "tcp"
        published: object
        target: object
        if isinstance(declaration, dict):
            published = declaration.get("published")
            target = declaration.get("target")
            host_ip = str(declaration.get("host_ip") or "")
            protocol = str(declaration.get("protocol") or "tcp")
        elif isinstance(declaration, str):
            value, separator, suffix = declaration.rpartition("/")
            if separator and suffix in {"tcp", "udp"}:
                declaration = value
                protocol = suffix
            parts = declaration.rsplit(":", 2)
            if len(parts) == 3:
                host_ip, published, target = parts
            elif len(parts) == 2:
                published, target = parts
            else:
                continue
        else:
            continue
        try:
            parsed.append(PublishedPort(host_ip, int(str(published)), int(str(target)), protocol))
        except (TypeError, ValueError):
            continue
    return parsed
