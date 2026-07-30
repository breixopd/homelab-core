from __future__ import annotations

from unittest.mock import patch

from tests.helpers.machines import machines_with_addresses
from toolkit.core.config.config import Config, ExternalHost
from toolkit.core.manifest.catalog import ServiceCatalog, load_service_catalog
from toolkit.core.registry.mesh import headscale_acl_tags, mesh_lan_cidr


def test_headscale_acl_tags_include_canonical_fleet_node_tags() -> None:
    cfg = Config(
        fleet={"headscale_tags": ["tag:fleet"]},
        external_hosts=[
            ExternalHost(
                name="edge-01",
                ip="192.0.2.10",
                kind="fleet",
                headscale_tags=["tag:edge", "tag:fleet"],
            ),
            ExternalHost(name="nas-01", ip="192.0.2.11"),
        ],
    )

    assert headscale_acl_tags(cfg) == ["tag:edge", "tag:fleet", "tag:homelab-router"]


def test_mesh_lan_cidr_is_derived_from_mesh_router_machine_prefix() -> None:
    machines = machines_with_addresses(infra="10.20.19.10", media="10.20.19.11", apps="10.20.19.12")
    machines = {
        name: machine.model_copy(update={"cidr": 20, "gateway": "10.20.16.1"}) for name, machine in machines.items()
    }

    assert mesh_lan_cidr(Config(machines=machines)) == "10.20.16.0/20"


def test_mesh_lan_cidr_resolves_the_declared_mesh_router_capability() -> None:
    machines = machines_with_addresses(infra="10.20.19.10", media="10.30.29.10", apps="10.20.19.12")
    machines = {
        name: machine.model_copy(update={"cidr": 20, "gateway": address.rsplit(".", 1)[0] + ".1"})
        for name, machine in machines.items()
        for address in [machine.address]
    }
    existing = load_service_catalog()
    headscale = existing.require("headscale")
    alternate_router = headscale.model_copy(update={"name": "alternate-router", "provides": ("mesh-router",)})
    catalog = ServiceCatalog(
        tuple(alternate_router if manifest.name == "headscale" else manifest for manifest in existing.manifests)
    )

    with patch("toolkit.core.manifest.catalog.load_service_catalog", return_value=catalog):
        result = mesh_lan_cidr(Config(machines=machines))

    assert result == "10.20.16.0/20"
