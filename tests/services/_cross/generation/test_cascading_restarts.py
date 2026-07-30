"""F3: health-aware cascading restarts — reverse-dependency map + cascade verify.

The reverse-dep builder is a PURE function (unit-tested without a Watchdog
instance). The cascade-verify wiring in heal() is exercised via a Watchdog
constructed with mocked docker + state (the _no_network_probes autouse fixture
patches time.sleep to a no-op so the 30s grace collapses instantly).
"""

from __future__ import annotations

from toolkit.core.ops.watchdog import build_reverse_dependency_map


def test_reverse_map_empty_input():
    assert build_reverse_dependency_map({}) == {}


def test_reverse_map_basic():
    forward = {
        "authelia": ["postgres", "redis"],
        "grafana": ["postgres", "prometheus"],
        "nextcloud": ["postgres", "redis"],
    }
    rev = build_reverse_dependency_map(forward)
    # postgres is depended on by authelia, grafana, nextcloud.
    assert set(rev["postgres"]) == {"authelia", "grafana", "nextcloud"}
    # redis is depended on by authelia, nextcloud.
    assert set(rev["redis"]) == {"authelia", "nextcloud"}
    # prometheus is depended on by grafana.
    assert set(rev["prometheus"]) == {"grafana"}
    # Consumers that are NEVER a dependency (no entry) appear as empty lists.
    assert rev.get("authelia", []) == []
    assert rev.get("grafana", []) == []


def test_reverse_map_deduplicates_consumers():
    # If the same consumer appears multiple times for a dep (shouldn't, but
    # be defensive), the reverse map lists it once.
    forward = {"a": ["x"], "b": ["x", "x"]}
    rev = build_reverse_dependency_map(forward)
    assert rev["x"] == ["a", "b"]


def test_reverse_map_preserves_all_nodes():
    # Every forward-map key must also appear in the reverse map (as a key),
    # so consumers with no reverse-deps get an empty list rather than KeyError.
    forward = {"http-echo": ["postgres"]}
    rev = build_reverse_dependency_map(forward)
    assert "http-echo" in rev
    assert rev["http-echo"] == []
    assert rev["postgres"] == ["http-echo"]


def test_check_dependency_connectivity_uses_discovered_graph(monkeypatch):
    """F3: the dep-connectivity check must derive consumers from the discovered
    dependency graph, NOT a hardcoded per-dep consumer list. Prove it by
    constructing a Watchdog whose reverse-dep graph names a NON-standard consumer
    (e.g. 'totally-custom-app' depends on 'postgres') and asserting that consumer
    gets probed."""
    from pathlib import Path
    from unittest.mock import MagicMock

    from toolkit.core.config.config import Config
    from toolkit.core.ops.watchdog import Watchdog

    root = Path("/tmp/nonexistent-homelab-f3")  # never touched; all probes mocked
    cfg = Config(domain="example.com", email="admin@example.com")
    wd = Watchdog.__new__(Watchdog)  # bypass __init__ (no docker/state needed)
    wd.root = root
    wd.config = cfg
    wd._discovered_deps = {"totally-custom-app": ["postgres"]}
    wd._discovered_safe = set()
    wd._discovered_careful = set()
    wd._discovered_blocked = set()

    # Mocked docker layer: 'postgres' + the custom consumer are both "running";
    # the connectivity probe returns unreachable → emit a HealthIssue for the
    # custom consumer. This proves the discovered consumer is probed.
    wd._get_running_names = lambda: {"postgres", "totally-custom-app"}
    wd._container_category = lambda name: "apps"
    wd._run = MagicMock(return_value=MagicMock(returncode=1, stdout="fail"))
    # Stub _reverse_dep_links' dependency on compose_dependency_map (which would
    # read the repo root). Override the property via __class__ monkeypatch would
    # be fragile; instead stub the stagger_planner import path.
    monkeypatch.setattr(
        "toolkit.core.registry.stagger_planner.compose_dependency_map",
        lambda root: {"totally-custom-app": ["postgres"]},
    )

    issues = wd.check_dependency_connectivity()

    # The custom consumer should be flagged as unreachable.
    custom_issues = [i for i in issues if i.service == "totally-custom-app"]
    assert len(custom_issues) == 1, (
        f"expected the discovered custom consumer to be probed; got: {[(i.service, i.message) for i in issues]}"
    )
    assert "5432" in custom_issues[0].message
    assert "postgres" in custom_issues[0].message
