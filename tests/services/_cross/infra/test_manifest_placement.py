from __future__ import annotations

import pytest
from tests.helpers.machines import enabled_machines, renamed_default_machines, single_control_machines
from toolkit.core.config.config import Config
from toolkit.core.manifest.catalog import ServiceCatalog, load_service_catalog
from toolkit.core.manifest.placement import (
    manifest_runtime_nodes,
    runtime_profiles_for_node,
    service_address,
    service_endpoint_port,
    service_is_local,
    service_node,
    service_route_port,
)
from toolkit.core.manifest.schema import RuntimeServiceManifest


def _renamed_machines():
    return renamed_default_machines()


def test_service_placement_comes_from_manifest() -> None:
    cfg = Config(domain="example.com")

    assert service_node(cfg, "postgres") == "infra"
    assert service_node(cfg, "vaultwarden") == "apps"
    assert service_address(cfg, "vaultwarden") == "10.10.10.12"
    assert service_is_local(cfg, "apps", "vaultwarden") is True
    assert service_is_local(cfg, "infra", "vaultwarden") is False


def test_single_node_collapses_every_service_to_control_machine() -> None:
    cfg = Config(domain="example.com", machines=single_control_machines())

    assert service_node(cfg, "vaultwarden") == "infra"
    assert service_is_local(cfg, "infra", "vaultwarden") is True


def test_service_targeting_disabled_machine_fails_closed() -> None:
    cfg = Config(domain="example.com", machines=enabled_machines("infra", "media"))

    with pytest.raises(ValueError, match="matches no enabled machine"):
        service_node(cfg, "vaultwarden")


def test_service_placement_resolves_machine_labels_after_ids_are_renamed() -> None:
    cfg = Config(domain="example.com", machines=_renamed_machines())

    assert cfg.enabled_nodes == ["core", "stream", "data"]
    assert service_node(cfg, "postgres") == "core"
    assert service_node(cfg, "music-sync") == "stream"
    assert service_node(cfg, "vaultwarden") == "data"


def test_manifest_endpoint_contracts_are_the_only_port_source() -> None:
    assert service_endpoint_port("lldap") == 3890
    assert service_endpoint_port("lldap", published=True) == 3890
    assert service_route_port("lldap", subdomain="users") == 17170
    assert service_route_port("vaultwarden", published=True) == 8082


def test_custom_machine_ids_drive_all_manifest_placements() -> None:
    defaults = renamed_default_machines()
    machines = {
        "control-plane": defaults["core"].model_copy(update={"hostname": "control-plane-01"}),
        "media-tier": defaults["stream"].model_copy(update={"hostname": "media-tier-01"}),
        "apps-tier": defaults["data"].model_copy(update={"hostname": "apps-tier-01"}),
    }
    cfg = Config(domain="example.com", machines=machines)

    assert cfg.enabled_nodes == ["control-plane", "media-tier", "apps-tier"]
    assert service_node(cfg, "postgres") == "control-plane"
    assert service_node(cfg, "music-sync") == "media-tier"
    assert service_node(cfg, "romm") == "apps-tier"


def test_service_placement_rejects_ambiguous_labels() -> None:
    machines = _renamed_machines()
    machines["data-two"] = machines["data"].model_copy(
        update={"hostname": "data-02", "vmid": 899, "address": "10.10.10.99"}
    )
    cfg = Config(domain="example.com", machines=machines)

    with pytest.raises(ValueError, match="matches multiple enabled machines"):
        service_node(cfg, "vaultwarden")


def test_non_primary_runtime_selector_expands_to_new_machines() -> None:
    machines = _renamed_machines()
    machines["worker"] = machines["data"].model_copy(
        update={
            "hostname": "worker-01",
            "vmid": 899,
            "address": "10.10.10.99",
            "labels": ("worker",),
        }
    )
    cfg = Config(domain="example.com", machines=machines)
    manifest = load_service_catalog().require("node-exporter")

    assert manifest_runtime_nodes(cfg, manifest, "node-exporter-agent") == ("stream", "data", "worker")


def test_runtime_capability_selector_can_match_multiple_machines() -> None:
    machines = _renamed_machines()
    machines["stream"] = machines["stream"].model_copy(update={"labels": (*machines["stream"].labels, "compute")})
    machines["data"] = machines["data"].model_copy(update={"labels": (*machines["data"].labels, "compute")})
    cfg = Config(domain="example.com", machines=machines)
    manifest = (
        load_service_catalog()
        .require("node-exporter")
        .model_copy(update={"runtimes": {"node-exporter-agent": RuntimeServiceManifest(placements=("compute",))}})
    )

    assert manifest_runtime_nodes(cfg, manifest, "node-exporter-agent") == ("stream", "data")


def test_runtime_profiles_are_compiled_from_enabled_manifest_placements() -> None:
    cfg = Config(domain="example.com")
    catalog = load_service_catalog()

    assert runtime_profiles_for_node(cfg, catalog, "infra") == ()
    assert runtime_profiles_for_node(cfg, catalog, "apps") == ("monitoring-agent",)
    assert runtime_profiles_for_node(cfg, catalog, "media") == ("media-vpn", "monitoring-agent")

    manifests = []
    for manifest in catalog.manifests:
        if manifest.name == "kopia":
            agent = manifest.runtimes["kopia-agent"].model_copy(update={"compose_profile": "backup-agent"})
            manifest = manifest.model_copy(update={"runtimes": {"kopia-agent": agent}})
        manifests.append(manifest)
    custom_catalog = ServiceCatalog(tuple(manifests))

    assert "backup-agent" not in runtime_profiles_for_node(cfg, custom_catalog, "apps")
    backups = cfg.model_copy(update={"backups": cfg.backups.model_copy(update={"enabled": True})})
    assert runtime_profiles_for_node(backups, custom_catalog, "apps") == ("backup-agent", "monitoring-agent")
