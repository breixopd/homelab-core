from __future__ import annotations

from pathlib import Path

import yaml
from toolkit.core.config.config import Config
from toolkit.core.generate.compose_assemble import assemble_compose_text
from toolkit.core.generate.validate import ValidationReport, _validate_cross_vm_ingress


def _published_ports(service: dict) -> set[int]:
    ports: set[int] = set()
    for declaration in service.get("ports", []) or []:
        if isinstance(declaration, str):
            without_protocol = declaration.rsplit("/", 1)[0]
            parts = without_protocol.rsplit(":", 2)
            if len(parts) == 3 and parts[-2].isdigit():
                ports.add(int(parts[-2]))
        elif isinstance(declaration, dict):
            ports.add(int(declaration["published"]))
    return ports


def test_every_cross_vm_route_has_a_guest_ip_published_port() -> None:
    root = Path.cwd()
    cfg = Config(domain="example.com", service_settings={"gluetun": {"enabled": True}})
    report = ValidationReport()

    _validate_cross_vm_ingress(root, cfg, report)

    assert report.errors == []
    assert "cross-VM ingress routes are reachable" in report.checks


def test_every_hardware_media_variant_publishes_the_canonical_ingress_port() -> None:
    compose = yaml.safe_load(assemble_compose_text(Path.cwd()))
    services = compose["services"]

    for service in ("jellyfin", "jellyfin-nvidia", "jellyfin-vaapi"):
        assert 8096 in _published_ports(services[service]), service
    for service in ("plex", "plex-nvidia", "plex-vaapi"):
        assert 32400 in _published_ports(services[service]), service
