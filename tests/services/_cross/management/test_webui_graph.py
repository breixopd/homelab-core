"""Controller-owned topology assembly for the Web UI graph and catalog."""

from __future__ import annotations

from pathlib import Path

from toolkit.controller.inventory_api import read_service_topology
from toolkit.controller.read_models import ContainerInventory, ContainerStatus
from toolkit.core.registry.service_graph import ServiceGraph, ServiceNode


def _toy_graph() -> ServiceGraph:
    return ServiceGraph(
        nodes={
            "postgres": ServiceNode(name="postgres", image="postgres:17", depends_on=()),
            "authelia": ServiceNode(
                name="authelia",
                image="authelia:4",
                depends_on=("postgres", "redis"),
            ),
            "redis": ServiceNode(name="redis", image="redis:8", depends_on=()),
            "grafana": ServiceNode(name="grafana", image="grafana:12", depends_on=("postgres",)),
        }
    )


def _patch_sources(monkeypatch, *, watchdog=None, metadata=None, containers=None) -> None:
    monkeypatch.setattr("toolkit.controller.inventory_api._load_graph", lambda _root: _toy_graph())
    monkeypatch.setattr(
        "toolkit.controller.inventory_api._load_watchdog_state",
        lambda _root: watchdog or {},
    )
    monkeypatch.setattr(
        "toolkit.controller.inventory_api._load_all_services",
        lambda: metadata or {},
    )
    monkeypatch.setattr(
        "toolkit.controller.inventory_api.read_container_inventory",
        lambda _root: ContainerInventory(
            is_available=True,
            unavailable_nodes=[],
            containers=containers or [],
        ),
    )


def test_topology_contains_dependency_edges_and_health(monkeypatch, tmp_path: Path) -> None:
    _patch_sources(
        monkeypatch,
        watchdog={
            "notify_state": {
                "postgres|db|down": {"terminal": True, "severity": "critical"},
                "authelia|sso|flap": {"terminal": False, "severity": "warning"},
            }
        },
    )

    topology = read_service_topology(tmp_path)
    health = {node.name: node.health for node in topology.nodes}
    edges = {(edge.source, edge.target) for edge in topology.edges}

    assert health["postgres"] == "critical"
    assert health["authelia"] == "warning"
    assert health["redis"] == "healthy"
    assert ("authelia", "postgres") in edges
    assert ("authelia", "redis") in edges


def test_topology_enriches_catalog_with_metadata_and_runtime(monkeypatch, tmp_path: Path) -> None:
    _patch_sources(
        monkeypatch,
        metadata={
            "grafana": {
                "label": "Grafana",
                "description": "Dashboards",
                "node": "infra",
                "memory_tier": "medium",
                "category": "management",
                "icon": "chart",
            }
        },
        containers=[
            ContainerStatus(
                name="grafana",
                node="infra",
                status="Up 1 hour (healthy)",
                state="running",
                image="grafana/grafana:12.0.0",
                health="healthy",
            )
        ],
    )

    topology = read_service_topology(tmp_path)
    catalog = {entry.name: entry for entry in topology.catalog}
    nodes = {node.name: node for node in topology.nodes}

    assert catalog["grafana"].label == "Grafana"
    assert catalog["grafana"].health == "healthy"
    assert catalog["grafana"].image == "grafana/grafana:12.0.0"
    assert nodes["grafana"].category == "management"
    assert catalog["postgres"].health == "unknown"
