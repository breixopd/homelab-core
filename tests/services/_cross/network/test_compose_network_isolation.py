"""Contract tests for compiler-derived Compose network isolation.

These use a single configured node so Caddy and every routed runtime are in the
same role model.  The assertions intentionally inspect generated Compose rather
than service source files: network topology is a compiler responsibility.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from toolkit.core.config.config import Config
from toolkit.core.generate.compose_assemble import assemble_role_compose_text
from toolkit.core.manifest.catalog import load_service_catalog


def _single_node_config() -> Config:
    defaults = Config(domain="example.com")
    return Config(
        domain="example.com",
        machines={"infra": defaults.machines["infra"]},
    )


def _model() -> dict[str, Any]:
    cfg = _single_node_config()
    return yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "infra"))


def _service_networks(model: dict[str, Any], service: str) -> set[str]:
    networks = model["services"][service].get("networks", {})
    if isinstance(networks, dict):
        return set(networks)
    return set(networks)


def test_role_model_has_no_shared_edge_and_keeps_runtime_networks_isolated() -> None:
    model = _model()
    networks = model["networks"]

    assert "edge" not in networks
    assert all("edge" not in _service_networks(model, service) for service in model["services"])

    # Each plugin owns a private runtime network.  A public media runtime must
    # not become a lateral path into an unrelated private processor.
    assert _service_networks(model, "jellyfin") & {"plugin-jellyfin"}
    assert _service_networks(model, "tdarr") & {"plugin-tdarr"}
    assert _service_networks(model, "jellyfin").isdisjoint(_service_networks(model, "tdarr"))


def test_ingress_is_host_routed_and_does_not_create_lateral_network_access() -> None:
    model = _model()
    caddy_networks = _service_networks(model, "caddy")

    # Ingress reaches host-published ports, so the edge proxy never shares a
    # bridge with application runtimes (which would permit lateral bypasses).
    for runtime in ("fmd-server", "romm", "jellyfin", "tdarr"):
        assert caddy_networks.isdisjoint(_service_networks(model, runtime))
        ports = model["services"][runtime].get("ports")
        assert isinstance(ports, list) and ports, f"{runtime} must publish its declared route port"

    # Keep this invariant data-driven for future service plugins: every
    # upstream that is actually present in the role model gets a host endpoint
    # for Caddy, regardless of whether it was added after this test was written.
    for manifest in load_service_catalog().manifests:
        for route in manifest.routes:
            runtime = route.compose_service or route.upstream.partition(":")[0]
            if not runtime or runtime == "caddy" or runtime not in model["services"]:
                continue
            ports = model["services"][runtime].get("ports")
            assert isinstance(ports, list) and ports, f"{manifest.name} route runtime {runtime} lacks a host port"


def test_declared_database_and_integration_links_are_preserved_without_global_sharing() -> None:
    model = _model()

    # RomM declares both PostgreSQL and Redis in its service manifest.  The
    # compiler emits one link network per relationship, rather than putting all
    # applications and databases on a broad shared bridge.
    for dependency, network in (("postgres", "link-postgres-romm"), ("redis", "link-redis-romm")):
        assert network in model["networks"]
        assert network in _service_networks(model, "romm")
        assert network in _service_networks(model, dependency)
        members = {service for service in model["services"] if network in _service_networks(model, service)}
        assert members == {"romm", dependency}

    assert _service_networks(model, "romm").isdisjoint(_service_networks(model, "jellyfin"))


def test_manifest_dependencies_compile_to_scoped_runtime_links() -> None:
    model = _model()

    for left, right in (
        ("roundcube", "mailserver"),
        ("homelab-ui", "lldap"),
        ("immich-server", "immich-machine-learning"),
        ("recyclarr", "sonarr"),
        ("recyclarr", "radarr"),
        ("bazarr", "sonarr"),
        ("bazarr", "radarr"),
        ("prowlarr", "flaresolverr"),
        ("sonarr", "gluetun"),
        ("radarr", "gluetun"),
        ("sonarr", "qbittorrent"),
        ("radarr", "qbittorrent"),
    ):
        shared = _service_networks(model, left) & _service_networks(model, right)
        assert shared == {f"link-{'-'.join(sorted((left, right)))}"}


def test_metrics_links_are_scoped_to_prometheus_and_each_scrape_target() -> None:
    model = _model()

    # Prometheus also scrapes host-published ports.  It must not share a
    # bridge with exporters, otherwise any exporter compromise reaches the
    # metrics control plane.
    prometheus_networks = _service_networks(model, "prometheus")
    for target in ("node-exporter", "cadvisor"):
        assert prometheus_networks.isdisjoint(_service_networks(model, target))
