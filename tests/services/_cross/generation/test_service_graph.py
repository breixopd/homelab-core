"""Tests for compose-derived ServiceGraph parsing and topo layers."""

from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.core.registry.service_graph import ServiceGraph, ServiceNode

MINIMAL_COMPOSE = """\
services:
  postgres:
    image: postgres:16
    profiles: [infra]
    ports:
      - "5432:5432"
    x-homelab:
      watchdog: {restart-policy: careful}

  redis:
    image: redis:7
    profiles:
      - infra

  api:
    image: example/api:latest
    profiles: [apps]
    depends_on:
      postgres:
        condition: service_healthy
      redis: {condition: service_started}
    ports:
      - target: 8080
        published: 8080
        host_ip: 127.0.0.1
      - "127.0.0.1:9090:9090"
    labels:
      x-homelab.verify: "true"
"""


@pytest.fixture
def compose_file(tmp_path: Path) -> Path:
    path = tmp_path / "docker-compose.yml"
    path.write_text(MINIMAL_COMPOSE)
    return path


def test_from_compose_parses_service_fields(compose_file: Path):
    graph = ServiceGraph.from_compose(compose_file)

    postgres = graph.nodes["postgres"]
    assert postgres.image == "postgres:16"
    assert postgres.profiles == ("infra",)
    assert postgres.depends_on == ()
    assert postgres.ports[0].published == "5432"
    assert postgres.ports[0].target == "5432"
    assert postgres.labels == {"watchdog": {"restart-policy": "careful"}}

    api = graph.nodes["api"]
    assert api.depends_on == ("postgres", "redis")
    assert api.ports[0].published == "8080"
    assert api.ports[0].target == "8080"
    assert api.ports[0].host_ip == "127.0.0.1"
    assert api.ports[1].host_ip == "127.0.0.1"
    assert api.ports[1].published == "9090"
    assert api.labels == {"x-homelab.verify": "true"}


def test_service_names_preserves_compose_order(compose_file: Path):
    graph = ServiceGraph.from_compose(compose_file)
    assert graph.service_names() == ["postgres", "redis", "api"]


def test_topo_layers_respects_depends_on(compose_file: Path):
    graph = ServiceGraph.from_compose(compose_file)
    assert graph.topo_layers() == [["postgres", "redis"], ["api"]]


def test_topo_layers_detects_cycles(tmp_path: Path):
    path = tmp_path / "docker-compose.yml"
    path.write_text("services:\n  a:\n    depends_on: [b]\n  b:\n    depends_on: [a]\n")
    graph = ServiceGraph.from_compose(path)
    with pytest.raises(ValueError, match="cycle"):
        graph.topo_layers()


def test_filter_by_profiles():
    graph = ServiceGraph(
        nodes={
            "always": ServiceNode(name="always"),
            "infra": ServiceNode(name="infra", profiles=("infra",)),
            "media": ServiceNode(name="media", profiles=("media",)),
        }
    )
    filtered = graph.filter_by_profiles(frozenset({"infra"}))
    assert set(filtered.nodes) == {"always", "infra"}


def test_dependency_map_limits_to_graph_nodes(compose_file: Path):
    graph = ServiceGraph.from_compose(compose_file)
    assert graph.dependency_map()["api"] == ["postgres", "redis"]
    assert graph.dependency_map()["postgres"] == []
