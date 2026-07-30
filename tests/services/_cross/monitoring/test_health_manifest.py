"""Manifest-owned dependency health contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from toolkit.core.config.service_metadata import service_endpoint_ports
from toolkit.core.manifest.schema import HealthManifest, ServiceManifest


def _service(values: dict) -> ServiceManifest:
    return ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Example service",
            "icon": "box",
            "category": "cloud",
            "placement": "apps",
            "priority": 50,
            **values,
        }
    )


def test_health_block_defaults_to_no_public_probe() -> None:
    assert HealthManifest().public_probe_path == ""


def test_service_declares_a_typed_endpoint() -> None:
    service = _service({"service_endpoint": {"container_port": 8080, "published_port": 9080}})

    assert service.service_endpoint is not None
    assert service.service_endpoint.container_port == 8080
    assert service.service_endpoint.published_port == 9080


def test_health_block_declares_a_public_probe_path() -> None:
    service = _service(
        {
            "routes": [
                {
                    "upstream": "example:8080",
                    "exposure": "public",
                    "auth": {"mode": "native"},
                }
            ],
            "health": {"public_probe_path": "/api/health"},
        }
    )

    assert service.health.public_probe_path == "/api/health"


def test_health_public_probe_requires_a_default_public_route() -> None:
    with pytest.raises(ValidationError, match="default public route"):
        _service({"health": {"public_probe_path": "/health"}})


@pytest.mark.parametrize("value", [0, 65_536])
def test_service_endpoint_rejects_invalid_ports(value: int) -> None:
    with pytest.raises(ValidationError):
        _service({"service_endpoint": {"container_port": value}})


def test_health_block_rejects_removed_or_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _service({"health": {"check_interval_override": 10}})


def test_repository_service_endpoints_are_plugin_owned() -> None:
    ports = service_endpoint_ports()

    assert ports["postgres"] == 5432
    assert ports["redis"] == 6379
    assert ports["immich-postgres"] == 5432
    assert ports["loki"] == 3100
